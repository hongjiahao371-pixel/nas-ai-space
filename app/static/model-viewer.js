function parseObj(text) {
  const source = [];
  const triangles = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith('v ')) {
      const values = line.slice(2).trim().split(/\s+/).slice(0, 3).map(Number);
      if (values.length === 3 && values.every(Number.isFinite)) source.push(values);
    } else if (line.startsWith('f ')) {
      const indices = line.slice(2).trim().split(/\s+/).map(value => {
        const index = Number(value.split('/')[0]);
        return index < 0 ? source.length + index : index - 1;
      }).filter(index => Number.isInteger(index) && source[index]);
      for (let index = 1; index + 1 < indices.length; index += 1) {
        triangles.push(source[indices[0]], source[indices[index]], source[indices[index + 1]]);
      }
    }
  }
  return triangles;
}

function parsePly(text) {
  const lines = text.split(/\r?\n/);
  if (lines[0]?.trim() !== 'ply') throw new Error('不是有效的 PLY 文件');
  const end = lines.findIndex(line => line.trim() === 'end_header');
  if (end < 0 || !lines.slice(0, end).some(line => line.trim() === 'format ascii 1.0')) {
    throw new Error('当前支持 ASCII PLY 预览');
  }
  let vertexCount = 0;
  let faceCount = 0;
  for (const line of lines.slice(0, end)) {
    const parts = line.trim().split(/\s+/);
    if (parts[0] === 'element' && parts[1] === 'vertex') vertexCount = Number(parts[2]) || 0;
    if (parts[0] === 'element' && parts[1] === 'face') faceCount = Number(parts[2]) || 0;
  }
  const vertices = lines.slice(end + 1, end + 1 + vertexCount).map(line => (
    line.trim().split(/\s+/).slice(0, 3).map(Number)
  ));
  const triangles = [];
  for (const line of lines.slice(end + 1 + vertexCount, end + 1 + vertexCount + faceCount)) {
    const values = line.trim().split(/\s+/).map(Number);
    const indices = values.slice(1, 1 + values[0]).filter(index => vertices[index]);
    for (let index = 1; index + 1 < indices.length; index += 1) {
      triangles.push(vertices[indices[0]], vertices[indices[index]], vertices[indices[index + 1]]);
    }
  }
  return triangles;
}

function normalizeTriangles(triangles) {
  if (!triangles.length) throw new Error('模型中没有可显示的三角面');
  const minimum = [Infinity, Infinity, Infinity];
  const maximum = [-Infinity, -Infinity, -Infinity];
  for (const vertex of triangles) {
    for (let axis = 0; axis < 3; axis += 1) {
      minimum[axis] = Math.min(minimum[axis], vertex[axis]);
      maximum[axis] = Math.max(maximum[axis], vertex[axis]);
    }
  }
  const center = minimum.map((value, axis) => (value + maximum[axis]) / 2);
  const scale = Math.max(...maximum.map((value, axis) => value - minimum[axis])) || 1;
  const positions = [];
  const normals = [];
  for (let index = 0; index < triangles.length; index += 3) {
    const points = triangles.slice(index, index + 3).map(vertex => vertex.map((value, axis) => (
      (value - center[axis]) * 2 / scale
    )));
    const left = points[1].map((value, axis) => value - points[0][axis]);
    const right = points[2].map((value, axis) => value - points[0][axis]);
    const normal = [
      left[1] * right[2] - left[2] * right[1],
      left[2] * right[0] - left[0] * right[2],
      left[0] * right[1] - left[1] * right[0],
    ];
    const length = Math.hypot(...normal) || 1;
    normal.forEach((value, axis) => { normal[axis] = value / length; });
    for (const point of points) {
      positions.push(...point);
      normals.push(...normal);
    }
  }
  return { positions: new Float32Array(positions), normals: new Float32Array(normals) };
}

function shader(gl, type, source) {
  const value = gl.createShader(type);
  gl.shaderSource(value, source);
  gl.compileShader(value);
  if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(value));
  return value;
}

function program(gl) {
  const value = gl.createProgram();
  gl.attachShader(value, shader(gl, gl.VERTEX_SHADER, `
    attribute vec3 position;
    attribute vec3 normal;
    uniform mat4 transform;
    uniform mat4 rotation;
    varying vec3 lighting;
    void main() {
      vec3 n = normalize((rotation * vec4(normal, 0.0)).xyz);
      vec3 key = normalize(vec3(0.45, 0.8, 0.6));
      float diffuse = max(dot(n, key), 0.0);
      lighting = vec3(0.23) + vec3(0.72, 0.76, 1.0) * diffuse;
      gl_Position = transform * vec4(position, 1.0);
    }
  `));
  gl.attachShader(value, shader(gl, gl.FRAGMENT_SHADER, `
    precision mediump float;
    varying vec3 lighting;
    void main() {
      gl_FragColor = vec4(vec3(0.48, 0.56, 0.96) * lighting, 1.0);
    }
  `));
  gl.linkProgram(value);
  if (!gl.getProgramParameter(value, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(value));
  return value;
}

function multiply(left, right) {
  const result = new Float32Array(16);
  for (let column = 0; column < 4; column += 1) {
    for (let row = 0; row < 4; row += 1) {
      result[column * 4 + row] = [0, 1, 2, 3].reduce((sum, index) => (
        sum + left[index * 4 + row] * right[column * 4 + index]
      ), 0);
    }
  }
  return result;
}

function rotation(x, y) {
  const cosX = Math.cos(x);
  const sinX = Math.sin(x);
  const cosY = Math.cos(y);
  const sinY = Math.sin(y);
  return multiply(
    new Float32Array([cosY, 0, -sinY, 0, 0, 1, 0, 0, sinY, 0, cosY, 0, 0, 0, 0, 1]),
    new Float32Array([1, 0, 0, 0, 0, cosX, sinX, 0, 0, -sinX, cosX, 0, 0, 0, 0, 1]),
  );
}

function projection(aspect, distance) {
  const near = 0.1;
  const far = 100;
  const value = 1 / Math.tan(Math.PI / 8);
  const perspective = new Float32Array([
    value / aspect, 0, 0, 0,
    0, value, 0, 0,
    0, 0, (far + near) / (near - far), -1,
    0, 0, 2 * far * near / (near - far), 0,
  ]);
  const translate = new Float32Array([
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, -distance, 1,
  ]);
  return multiply(perspective, translate);
}

export async function mountModelViewer(canvas, url, fileName) {
  const response = await fetch(url);
  if (!response.ok) throw new Error('模型文件读取失败');
  if (Number(response.headers.get('content-length') || 0) > 80 * 1024 * 1024) {
    throw new Error('浏览器预览暂时限制为 80 MB 以内的模型');
  }
  const text = await response.text();
  const triangles = /\.ply$/i.test(fileName) ? parsePly(text) : parseObj(text);
  const geometry = normalizeTriangles(triangles);
  const gl = canvas.getContext('webgl', { antialias: true, alpha: false });
  if (!gl) throw new Error('当前浏览器不支持 WebGL');
  const pipeline = program(gl);
  const buffers = [];
  const bind = (name, values) => {
    const buffer = gl.createBuffer();
    buffers.push(buffer);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, values, gl.STATIC_DRAW);
    const location = gl.getAttribLocation(pipeline, name);
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0);
  };
  gl.useProgram(pipeline);
  bind('position', geometry.positions);
  bind('normal', geometry.normals);
  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.CULL_FACE);
  let angleX = -0.25;
  let angleY = 0.55;
  let distance = 4.5;
  let drag = null;
  const draw = () => {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    gl.viewport(0, 0, width, height);
    gl.clearColor(0.025, 0.035, 0.055, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const modelRotation = rotation(angleX, angleY);
    gl.uniformMatrix4fv(gl.getUniformLocation(pipeline, 'rotation'), false, modelRotation);
    gl.uniformMatrix4fv(
      gl.getUniformLocation(pipeline, 'transform'),
      false,
      multiply(projection(width / height, distance), modelRotation),
    );
    gl.drawArrays(gl.TRIANGLES, 0, geometry.positions.length / 3);
  };
  canvas.addEventListener('pointerdown', event => {
    drag = { x: event.clientX, y: event.clientY, angleX, angleY };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag) return;
    angleY = drag.angleY + (event.clientX - drag.x) * 0.01;
    angleX = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, drag.angleX + (event.clientY - drag.y) * 0.01));
    draw();
  });
  canvas.addEventListener('pointerup', () => { drag = null; });
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    distance = Math.max(2.6, Math.min(9, distance + event.deltaY * 0.003));
    draw();
  }, { passive: false });
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
  draw();

  // 返回释放函数：原生 WebGL 没有 renderer，删 buffer/program 后借 WEBGL_lose_context 主动丢上下文
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    observer.disconnect();
    buffers.forEach(buffer => gl.deleteBuffer(buffer));
    gl.deleteProgram(pipeline);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  };
}
