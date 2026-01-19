const API_BASE = import.meta.env.VITE_API_BASE || '/api';

export async function getJson(path) {
  const resp = await fetch(`${API_BASE}${path}`, { cache: 'no-store' });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || resp.statusText || 'Request failed');
  }
  return resp.json();
}

export async function postJson(path, payload) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {})
  });
  const text = await resp.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    throw new Error(text || resp.statusText || 'Request failed');
  }
  if (!resp.ok) {
    throw new Error(data?.error || resp.statusText || 'Request failed');
  }
  return data;
}

export async function deleteJson(path) {
  const resp = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || resp.statusText || 'Request failed');
  }
  return resp.json();
}

export async function uploadForm(path, formData) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    body: formData
  });
  if (!resp.ok) {
    let errorMsg = 'Upload failed';
    try {
      const err = await resp.json();
      errorMsg = err?.error || errorMsg;
    } catch (err) {
      const text = await resp.text();
      errorMsg = text || resp.statusText || errorMsg;
    }
    throw new Error(errorMsg);
  }
  return resp.json();
}
