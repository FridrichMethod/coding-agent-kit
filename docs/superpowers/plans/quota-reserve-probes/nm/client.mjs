// NM Node egress client: node client.mjs SPEC_JSON (loopback fixtures only).
// Attempts: {name, kind: tcp4|tcp6|udp4|fetch, host, port, url}. Prints JSON.
import net from 'node:net';
import dgram from 'node:dgram';

const spec = JSON.parse(process.argv[2]);

function tcp(host, port) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port, timeout: 3000 });
    sock.on('connect', () => { sock.end('NM-NODE-PROBE\n'); resolve('connected'); });
    sock.on('timeout', () => { sock.destroy(); resolve('error:timeout'); });
    sock.on('error', (e) => resolve('error:' + (e.code || e.message)));
  });
}

function udp(host, port) {
  return new Promise((resolve) => {
    const sock = dgram.createSocket('udp4');
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      try { sock.close(); } catch { /* already closed */ }
      resolve(value);
    };
    sock.on('error', (e) => finish('error:' + (e.code || e.message)));
    sock.send('NM-NODE-UDP', port, host, (e) => finish(e ? 'error:' + (e.code || e.message) : 'sent'));
  });
}

async function fetchUrl(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return 'status:' + res.status;
  } catch (e) {
    return 'error:' + ((e.cause && e.cause.code) || e.name);
  }
}

const results = {};
for (const a of spec.attempts) {
  if (a.kind === 'tcp4' || a.kind === 'tcp6') results[a.name] = await tcp(a.host, a.port);
  else if (a.kind === 'udp4') results[a.name] = await udp(a.host, a.port);
  else if (a.kind === 'fetch') results[a.name] = await fetchUrl(a.url);
  else results[a.name] = 'error:unknown-kind';
}
console.log(JSON.stringify({ pid: process.pid, results }));
