# Third-party notices

NAS AI Space is licensed under Apache-2.0. The following components are
distributed with, downloaded by, or referenced by the project under their own
licenses. Those licenses continue to apply to the corresponding components.

## Files distributed in this repository

| Component | Location | License | Source |
|---|---|---|---|
| Three.js r180 | `app/static/vendor/three/` | MIT | https://github.com/mrdoob/three.js |
| OpenCV Zoo YuNet model | `models/face_detection_yunet_2023mar.onnx` | MIT | https://huggingface.co/opencv/face_detection_yunet |
| OpenCV Zoo SFace model | `models/face_recognition_sface_2021dec.onnx` | Apache-2.0 | https://huggingface.co/opencv/face_recognition_sface |

The model license texts are preserved in `models/LICENSE-YUNET` and
`models/LICENSE-SFACE`. Three.js source files retain their upstream license
headers; the upstream MIT license applies to the vendored files.

## Runtime components and models

The default Compose stack downloads or runs external components such as
Ollama, Qdrant and Speaches. It also downloads the models selected in `.env`
(by default Qwen3 Embedding, Qwen3-VL and faster-whisper). These artifacts are
not relicensed by NAS AI Space. Before redistribution or commercial use,
review the license and usage terms published by the exact image or model
version you select.

Changing a model name, image tag or hardware-specific runtime may change the
applicable third-party terms. Keep this notice and the corresponding upstream
license files when redistributing a combined image or appliance.
