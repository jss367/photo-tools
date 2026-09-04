# Pose model export environments

Vireo runs exported ONNX models with NumPy 2. Its optional pose-model
conversion tools require NumPy 1 and must run in separate environments.
They are not part of the application's `export` extra.

Run these commands from the repository root. Use Python 3.11 for these
conversion environments and pass the environment's interpreter explicitly;
do not install Vireo itself into them.

For SuperAnimal bird or quadruped models:

```bash
uv venv --python 3.11 .venv-superanimal-export
uv pip install --python .venv-superanimal-export/bin/python -r scripts/model-export/superanimal-requirements.txt
.venv-superanimal-export/bin/python scripts/export_onnx.py --model superanimal-bird --validate
.venv-superanimal-export/bin/python scripts/export_onnx.py --model superanimal-quadruped --validate
```

For RTMPose animal keypoints, use an x86-64 Linux or Windows machine.
MMDeploy 1.3 distributes wheels for those platforms; these instructions
do not support Apple Silicon. Install the build dependencies first so
MMCV can compile its PyTorch operators and Chumpy can import `pip`:

```bash
uv venv --python 3.11 .venv-rtmpose-export
uv pip install --python .venv-rtmpose-export/bin/python -r scripts/model-export/rtmpose-build-requirements.txt
uv pip install --python .venv-rtmpose-export/bin/python -r scripts/model-export/rtmpose-requirements.txt --no-build-isolation
.venv-rtmpose-export/bin/python scripts/export_onnx.py --model rtmpose-animal --validate
```

On Windows, use `Scripts/python.exe` instead of `bin/python`. MMCV includes
native extensions; its build requires a compiler. The bootstrap pins PyTorch
for this older converter stack, and setuptools retains the `pkg_resources`
module required by [MMCV's build script](https://github.com/open-mmlab/mmcv/blob/v2.1.0/setup.py).
Model conversion also downloads model weights.

Other models continue to use `pip install -e ".[export]"`. Export individual
models in the appropriate environment; `--all` attempts every model, including
the pose exporters that are installed separately. Generated ONNX files can
be used by Vireo's normal environment without either pose framework installed.
