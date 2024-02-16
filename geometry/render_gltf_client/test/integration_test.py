"""Conducts integration tests by exercising the entire RPC pipeline that
contains a render client and a render server.  The tests then inspect the
generated images or glTF files by comparing them with the expected output.

As different renderers may have various rounding errors or subtle lighting
settings, two heuristic thresholds are applied, i.e., a per-pixel threshold for
each image type and an overall percentage threshold for invalid pixels.

Two images are considered close enough if the majority of the pixels are within
the per-pixel threshold.  The ground truth images are generated by
RenderEngineVtk.

For glTF comparison, the generated glTFs are compared against a carefully
inspected ground truth glTF file.  Some entries not in the scope of the glTF
test are replaced with placeholders to make the file size minimal.

See `Testing of the client-server RPC pipeline` section in the README file for
more information.
"""

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

from bazel_tools.tools.python.runfiles.runfiles import Create as CreateRunfiles
import numpy as np
from PIL import Image

COLOR_PIXEL_THRESHOLD = 20  # RGB pixel value tolerance.
DEPTH_PIXEL_THRESHOLD = 0.001  # Depth measurement tolerance in meters.
LABEL_PIXEL_THRESHOLD = 0
INVALID_PIXEL_FRACTION = 0.2

# TODO(#19305) Remove this once the skipped tests reliably pass on macOS.
_SKIP = False
if "darwin" in sys.platform:
    _SKIP = True


class TestIntegration(unittest.TestCase):
    def setUp(self):
        # Allow the testing log to print the nested dictionary for debugging
        # when the glTF test fails.
        self.maxDiff = None

        self.runfiles = CreateRunfiles()
        self.tmp_dir = os.environ.get("TEST_TMPDIR", "/tmp")

        server_demo = self.runfiles.Rlocation(
            "drake/geometry/render_gltf_client/server_demo"
        )

        # Start the server on the other process. Bind to port 0 and let the OS
        # assign an available port later on.
        server_args = [
            server_demo,
            "--host=127.0.0.1",
            "--port=0",
        ]
        self.server_proc = subprocess.Popen(
            server_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait to hear which port it's using.
        self.server_port = None
        while self.server_port is None:
            line = self.server_proc.stdout.readline().decode("utf-8")
            print(f"[server] {line}", file=sys.stderr, end="")
            match = re.search(r"Running on http://127.0.0.1:([0-9]+)", line)
            if match:
                (self.server_port,) = match.groups()

    def tearDown(self):
        self.server_proc.kill()

    def run_render_client(self, renderer, cleanup=True):
        """Invokes the client process to send rendering requests.  If cleanup
        is set to false, the client will keep the intermediate glTF files (and
        the images).
        """
        client_demo = self.runfiles.Rlocation(
            "drake/geometry/render_gltf_client/client_demo"
        )
        save_dir = os.path.join(self.tmp_dir, renderer)
        os.makedirs(save_dir, exist_ok=True)

        render_args = [
            client_demo,
            f"--render_engine={renderer}",
            "--simulation_time=0.1",
            f"--cleanup={cleanup}",
            f"--save_dir={save_dir}",
            f"--server_base_url=127.0.0.1:{self.server_port}",
        ]
        result = subprocess.run(
            render_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
        )
        if result.returncode != 0:
            for line in result.stdout.splitlines():
                print(f"[client] {line}", file=sys.stderr)
        result.check_returncode()
        return result

    def get_demo_image_paths(self, renderer):
        """Returns the curated image paths.  The output folder is assumed to
        contain at least 2 sets of color, depth, and label images for
        inspection.
        """
        save_dir = os.path.join(self.tmp_dir, renderer)

        image_sets = []
        for index in range(2):
            image_set = [
                f"{save_dir}/color_{index:03d}.png",
                f"{save_dir}/depth_{index:03d}.tiff",
                f"{save_dir}/label_{index:03d}.png",
            ]
            image_sets.append(image_set)
        return image_sets

    def get_gltf_path_pairs(self, gltf_file_dir):
        """Returns pairs of generated and ground truth glTF files for each
        image type for inspection.
        """
        gltf_path_pairs = []
        for index, image_type in enumerate(["color", "depth", "label"]):
            gltf_path = f"{gltf_file_dir}/{index+1:019d}-{image_type}.gltf"
            ground_truth_gltf = self.runfiles.Rlocation(
                "drake/geometry/render_gltf_client/test/"
                f"test_{image_type}_scene.gltf"
            )
            gltf_path_pairs.append((gltf_path, ground_truth_gltf))
        return gltf_path_pairs

    def assert_error_fraction_less(self, image_diff, fraction):
        image_diff_fraction = np.count_nonzero(image_diff) / image_diff.size
        self.assertLess(image_diff_fraction, fraction)

    _REPLACED = {
        "bufferView": "bufferViews",
        "camera": "cameras",
        "index": "textures",
        "indices": "accessors",
        "material": "materials",
        "mesh": "meshes",
        "sampler": "samplers",
        "source": "images",
        "POSITION": "accessors",
        "TEXCOORD_0": "accessors",
    }
    """A map from an object property containing an index value to the name of
    the array that contains the full definition."""

    _REMOVED = ["buffer", "name"]
    """The nodes that we'll simply remove.  We don't want the test to even
    consider them."""

    @staticmethod
    def _traverse_and_mutate(gltf, entry):
        """Walks the tree rooted at `entry` and replace entries found in
        _REPLACED with the explicit tree referenced."""
        entry_type = type(entry)
        if entry_type == dict:
            for to_remove in TestIntegration._REMOVED:
                entry.pop(to_remove, None)
            for k, v in entry.items():
                # Replace the index with the actual referenced data structure.
                if k in TestIntegration._REPLACED.keys():
                    entry[k] = gltf[TestIntegration._REPLACED[k]][v]
                TestIntegration._traverse_and_mutate(gltf, entry[k])
        elif entry_type == list:
            # If the list contains only numeric numbers, round floating values
            # till 12 decimal places due to the precision differences across
            # platforms.
            if all(isinstance(x, (int, float)) for x in entry):
                for i in range(len(entry)):
                    entry[i] = round(entry[i], 12)
            elif not all(isinstance(x, str) for x in entry):
                for item in entry:
                    TestIntegration._traverse_and_mutate(gltf, item)
        # Everything else is something that doesn't need normalization.

    @staticmethod
    def _normalize_for_compare(gltf):
        """Creates an alternative gltf dictionary from `gltf` by normalizing it
        for platform-independent comparisons.  Most of the normalization takes
        place during the recursive traversal.  Normalization includes:
            - Elimination of nodes outside the scope of this test (see
              _REMOVED).
            - Replacing indices to referenced items with the full specification
              (eliminating dependencies on the orders those indexed items would
              appear in their lists).
            - Rounding arrays of numeric values (setting consistent precision
              for floating-point comparison).
        """
        result = copy.deepcopy(gltf)

        # We're not going to compare any of the raw data in `buffers``.
        result.pop("buffers", None)

        TestIntegration._traverse_and_mutate(result, result["nodes"])
        return result

    def _check_one_gltf(self, gltf, ground_truth_gltf):
        actual = self._normalize_for_compare(gltf)
        expected = self._normalize_for_compare(ground_truth_gltf)

        for entry in ["scene", "scenes", "asset"]:
            self.assertEqual(actual[entry], expected[entry])
        # The `nodes` entry has the rest of the information after the
        # normalization, e.g., meshes, cameras, materials, accessors, etc.
        self.assertCountEqual(actual["nodes"], expected["nodes"])

    @staticmethod
    def _save_to_outputs(source_file, prefix=''):
        """Writes the given source file to the undeclared outputs (if defined).
        If written, the files will be found in:
        bazel-testlogs/geometry/render_gltf_client/py/integration_test/test.outputs  # noqa
        """
        output_dir = os.getenv('TEST_UNDECLARED_OUTPUTS_DIR')
        if output_dir is not None:
            source_path = Path(source_file)
            shutil.copy(source_path,
                        os.path.join(output_dir, prefix + source_path.name))

    @unittest.skipIf(_SKIP, "Skipped on macOS, see #19305")
    def test_integration(self):
        """Quantitatively compares the images rendered by RenderEngineVtk and
        RenderEngineGltfClient via a fully exercised RPC pipeline.
        """
        self.run_render_client("vtk")
        vtk_image_sets = self.get_demo_image_paths("vtk")
        self.run_render_client("client")
        client_image_sets = self.get_demo_image_paths("client")

        for image_set in vtk_image_sets:
            for image_path in image_set:
                self._save_to_outputs(image_path, 'vtk_')

        for image_set in client_image_sets:
            for image_path in image_set:
                self._save_to_outputs(image_path, 'client_')

        for vtk_image_paths, client_image_paths in zip(
            vtk_image_sets, client_image_sets
        ):
            # Load the images and convert them to numpy arrays.
            vtk_color, vtk_depth, vtk_label = (
                np.array(Image.open(image_path))
                for image_path in vtk_image_paths
            )
            client_color, client_depth, client_label = (
                np.array(Image.open(image_path))
                for image_path in client_image_paths
            )

            # Convert uint8 images to float data type to avoid overflow during
            # calculation.
            color_diff = (
                np.absolute(
                    vtk_color.astype(float) - client_color.astype(float)
                )
                > COLOR_PIXEL_THRESHOLD
            )
            self.assert_error_fraction_less(color_diff, INVALID_PIXEL_FRACTION)

            # Set the infinite values in depth images to zero.
            vtk_depth[~np.isfinite(vtk_depth)] = 0.0
            client_depth[~np.isfinite(client_depth)] = 0.0
            depth_diff = (
                np.absolute(vtk_depth - client_depth) > DEPTH_PIXEL_THRESHOLD
            )
            self.assert_error_fraction_less(depth_diff, INVALID_PIXEL_FRACTION)

            # By convention, where RenderEngineVtk uses RenderLabel::kEmpty
            # (32766), RenderEngineGltfClient uses RenderLabel::kDontCare
            # (32764). The values are hard-coded intentionally to avoid having
            # the large dependency of pydrake. Keep the values in sync if we
            # ever change the implementation of RenderLabel.
            # Make them match to facilitate comparison.
            vtk_label[vtk_label == 32766] = 32764
            label_diff = (
                np.absolute(vtk_label - client_label) > LABEL_PIXEL_THRESHOLD
            )
            self.assert_error_fraction_less(label_diff, INVALID_PIXEL_FRACTION)

    @unittest.skipIf(_SKIP, "Skipped on macOS, see #19305")
    def test_gltf_conversion(self):
        """Checks that the fundamental structure of the generated glTF files is
        preserved.  The comparison of the exact texture information is not in
        the test's scope and is covered in the integration test above.
        """
        result = self.run_render_client("client", cleanup=False)

        # Scrape the directory for the glTF files.
        gltf_file_dir = None
        for line in result.stdout.splitlines():
            print(f"[client] {line}", file=sys.stderr)
            match = re.search(
                r".* scene exported to '(.*)/[0-9]+-color.gltf'", line
            )
            if match:
                (gltf_file_dir,) = match.groups()
                break
        self.assertIsNotNone(gltf_file_dir)

        # Iterate through each gltf file to compare against the ground truth.
        for gltf_path, ground_truth_gltf_path in self.get_gltf_path_pairs(
            gltf_file_dir
        ):
            self._save_to_outputs(gltf_path)
            self._save_to_outputs(ground_truth_gltf_path)
            with open(gltf_path, "r") as f:
                gltf = json.load(f)
            with open(ground_truth_gltf_path, "r") as g:
                ground_truth_gltf = json.load(g)
            with self.subTest(gltf_path=os.path.basename(gltf_path)):
                self._check_one_gltf(gltf, ground_truth_gltf)
