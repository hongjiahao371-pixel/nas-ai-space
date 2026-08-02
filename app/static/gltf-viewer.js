// GLB / glTF 预览：基于 vendor 目录下的 three.js（r180），完全本地渲染，不依赖任何外部资源
import * as THREE from './vendor/three/three.module.js';
import { GLTFLoader } from './vendor/three/GLTFLoader.js';
import { RoomEnvironment } from './vendor/three/RoomEnvironment.js';

const MAX_MODEL_BYTES = 80 * 1024 * 1024;

export async function mountGltfViewer(canvas, url, fileName) {
  const response = await fetch(url);
  if (!response.ok) throw new Error('模型文件读取失败');
  if (Number(response.headers.get('content-length') || 0) > MAX_MODEL_BYTES) {
    throw new Error('浏览器预览暂时限制为 80 MB 以内的模型');
  }
  const payload = /\.gltf$/i.test(fileName) ? await response.text() : await response.arrayBuffer();
  const loader = new GLTFLoader();
  let gltf;
  try {
    // resourcePath 置空：.gltf 若引用外部 .bin/贴图会在解析阶段报错，仅支持自包含 glTF
    gltf = await new Promise((resolve, reject) => loader.parse(payload, '', resolve, reject));
  } catch (error) {
    throw new Error(`glTF 解析失败：${error?.message || error}（含外部 .bin/贴图的 .gltf 请先转为 GLB）`);
  }
  const model = gltf.scene || gltf.scenes?.[0];
  if (!model) throw new Error('模型中没有可显示的场景');

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x06090e);
  // RoomEnvironment 提供基础环境反射，避免金属材质发黑
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromScene(new RoomEnvironment(), 0.04);
  scene.environment = environment.texture;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x30364a, 1.1));
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.6);
  keyLight.position.set(3, 5, 4);
  scene.add(keyLight);
  scene.add(model);

  // 与 OBJ/PLY 预览一致：先归一化到以包围盒中心为原点的视角
  const bounds = new THREE.Box3().setFromObject(model);
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const base = Math.max(size.x, size.y, size.z) || 1;
  const camera = new THREE.PerspectiveCamera(45, 1, base / 100, base * 100);
  const target = center.clone();
  let theta = 0.6;
  let phi = 1.15;
  let radius = base * 2.2;
  let drag = null;

  const draw = () => {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
    phi = Math.max(0.05, Math.min(Math.PI - 0.05, phi));
    radius = Math.max(base * 0.4, Math.min(base * 12, radius));
    camera.position.set(
      target.x + radius * Math.sin(phi) * Math.sin(theta),
      target.y + radius * Math.cos(phi),
      target.z + radius * Math.sin(phi) * Math.cos(theta),
    );
    camera.lookAt(target);
    renderer.render(scene, camera);
  };

  canvas.addEventListener('pointerdown', event => {
    drag = { x: event.clientX, y: event.clientY, theta, phi, pan: event.button === 2 || event.shiftKey, target: target.clone() };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag) return;
    if (drag.pan) {
      // 右键或 Shift 拖动：沿相机平面平移观察中心
      const scale = radius / Math.max(canvas.clientHeight, 1);
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      target.copy(drag.target)
        .addScaledVector(right, -(event.clientX - drag.x) * scale)
        .addScaledVector(up, (event.clientY - drag.y) * scale);
    } else {
      theta = drag.theta - (event.clientX - drag.x) * 0.01;
      phi = drag.phi - (event.clientY - drag.y) * 0.01;
    }
    draw();
  });
  canvas.addEventListener('pointerup', () => { drag = null; });
  canvas.addEventListener('contextmenu', event => event.preventDefault());
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    radius += event.deltaY * 0.003 * base;
    draw();
  }, { passive: false });
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
  draw();

  // 返回释放函数：关弹窗 / 切换版本前必须调用。
  // 浏览器 WebGL 上下文数量有限，detached canvas 不主动丢上下文会一直占着名额直至黑屏
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    observer.disconnect();
    environment.dispose();
    pmrem.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
  };
}
