// static/app.js
let ws = null;
let modeMouse = true;

const btn = document.getElementById('connectBtn');
const status = document.getElementById('status');
const screenImg = document.getElementById('screen');
const controls = document.getElementById('controls');

btn.onclick = () => {
  if (ws) return;
  const ip = document.getElementById('serverIp').value.trim();
  const code = document.getElementById('sessionCode').value.trim();
  const devName = document.getElementById('devName').value.trim() || 'browser';
  if (!ip || !code) { alert('IP y código requeridos'); return; }
  const url = `ws://${ip}:8080/ws`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    status.innerText = 'Socket abierto, enviando auth...';
    ws.send(JSON.stringify({type:'auth', device_name: devName, device_id: 'web-'+Math.random().toString(36).slice(2), code: code}));
  };
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === 'auth_result') {
        status.innerText = 'Auth: ' + data.status;
        if (data.status === 'accepted') {
          status.innerText = 'Conectado';
          screenImg.style.display = 'block';
          controls.style.display = 'flex';
        } else if (data.status === 'pending') {
          status.innerText = 'Pendiente: espera aprobación en la PC';
        } else if (data.status === 'banned') {
          status.innerText = 'Baneado por esta sesión';
        }
      } else if (data.type === 'frame') {
        // set image src to data URL
        screenImg.src = 'data:image/jpeg;base64,' + data.b64;
      } else if (data.type === 'permission_update') {
        console.log('Perms', data.permissions);
      }
    } catch (e) {
      console.error(e);
    }
  };
  ws.onclose = () => {
    status.innerText = 'Desconectado';
    ws = null;
    screenImg.style.display = 'none';
    controls.style.display = 'none';
  };
  ws.onerror = (e) => { status.innerText = 'Error socket'; console.error(e); };
};

// click on screen -> send mouse click relative coords
screenImg.onclick = (ev) => {
  if (!ws) return;
  const rect = screenImg.getBoundingClientRect();
  const rx = (ev.clientX - rect.left) / rect.width;
  const ry = (ev.clientY - rect.top) / rect.height;
  ws.send(JSON.stringify({type:'input_event', payload: {type:'mouse_click', x: rx, y: ry}}));
};

// send text
document.getElementById('sendText').onclick = () => {
  const t = document.getElementById('textBox').value;
  if (!ws) return alert('No conectado');
  ws.send(JSON.stringify({type:'input_event', payload:{type:'type_text', text: t}}));
};

// disconnect
document.getElementById('disconnect').onclick = () => {
  if (ws) ws.close();
  ws = null;
  status.innerText = 'Desconectado';
};

