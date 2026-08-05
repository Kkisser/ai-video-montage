#!/usr/bin/env python3
"""webapp.py — локальный веб-интерфейс для montage.py.

Возможности:
  • выбрать папку-источник клипов и папку-назначение (обзор папок в браузере);
  • список клипов: порядок (вверх/вниз), текст реплики, trim, вкл/выкл;
  • авто-имя результата: video1.mp4, video2.mp4, … (следующий свободный в папке);
  • запуск монтажа и живой лог прямо на странице.

Запуск:  ./venv/bin/python webapp.py   →  http://127.0.0.1:5001
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "bin" / "python"
MONTAGE = ROOT / "montage.py"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}

app = Flask(__name__)
jobs: dict[str, dict] = {}


# ---------- Папки и файлы ----------------------------------------------------
def list_dir(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_dir():
        path = Path.home()
    dirs, vids = [], []
    try:
        for p in sorted(path.iterdir(), key=lambda x: x.name.lower()):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                dirs.append(p.name)
            elif p.suffix.lower() in VIDEO_EXT:
                vids.append(p.name)
    except PermissionError:
        pass
    return {"path": str(path), "parent": str(path.parent),
            "dirs": dirs, "videos": vids}


def next_name(out_dir: Path) -> str:
    """Следующее свободное имя videoN.mp4 в папке."""
    n = 0
    for p in out_dir.glob("video*.mp4"):
        m = re.match(r"video(\d+)\.mp4$", p.name)
        if m:
            n = max(n, int(m.group(1)))
    return f"video{n + 1}.mp4"


# ---------- Запуск монтажа ---------------------------------------------------
def run_job(job_id: str, clips_path: Path, in_dir: str, out_file: str, model: str):
    job = jobs[job_id]
    cmd = [str(PY), str(MONTAGE), "--clips", str(clips_path),
           "--in", in_dir, "--out", out_file, "--model", model]
    job["log"] += "$ " + " ".join(cmd) + "\n"
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        job["proc"] = proc
        for line in proc.stdout:
            job["log"] += line
        proc.wait()
        job["ok"] = proc.returncode == 0
    except Exception as e:  # noqa: BLE001
        job["log"] += f"\nОШИБКА: {e}\n"
        job["ok"] = False
    finally:
        job["done"] = True


# ---------- API --------------------------------------------------------------
@app.get("/api/browse")
def api_browse():
    start = request.args.get("path") or str(ROOT)
    return jsonify(list_dir(Path(start)))


@app.get("/api/scan")
def api_scan():
    d = Path(request.args.get("path", "")).expanduser()
    vids = []
    if d.is_dir():
        vids = sorted(p.name for p in d.iterdir() if p.suffix.lower() in VIDEO_EXT)
    return jsonify({"videos": vids, "next": next_name(d if d.is_dir() else ROOT)})


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True)
    in_dir = Path(data["in_dir"]).expanduser()
    out_dir = Path(data["out_dir"]).expanduser()
    model = data.get("model", "medium")
    clips = [c for c in data["clips"] if c.get("include", True)]
    if not clips:
        return jsonify({"error": "Нет выбранных клипов"}), 400
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = {"clips": [{"file": c["file"], "text": c.get("text", ""),
                       "trim": c.get("trim", "normal")} for c in clips]}
    job_id = uuid.uuid4().hex[:8]
    clips_path = ROOT / "work" / f"clips_{job_id}.json"
    clips_path.parent.mkdir(exist_ok=True)
    clips_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2))

    out_name = next_name(out_dir)
    out_file = str(out_dir / out_name)
    jobs[job_id] = {"log": "", "done": False, "ok": None, "out_file": out_file}
    threading.Thread(target=run_job,
                     args=(job_id, clips_path, str(in_dir), out_file, model),
                     daemon=True).start()
    return jsonify({"job": job_id, "out_name": out_name, "out_file": out_file})


@app.get("/api/log")
def api_log():
    job = jobs.get(request.args.get("job", ""))
    if not job:
        return jsonify({"error": "нет задачи"}), 404
    return jsonify({"log": job["log"], "done": job["done"],
                    "ok": job["ok"], "out_file": job["out_file"]})


@app.get("/")
def index():
    return render_template_string(HTML, start=str(ROOT))


HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Автомонтаж — панель</title>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--tx:#e8eaed;--mut:#9aa0aa;
        --acc:#F0952A;--acc2:#B45309;--ok:#4caf50;--err:#e05656;}
  @media (prefers-color-scheme:light){:root{--bg:#f5f6f8;--card:#fff;--line:#e2e5ea;
        --tx:#1c1f26;--mut:#6b7280;}}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--tx)}
  .wrap{max-width:900px;margin:0 auto;padding:24px 18px 60px}
  h1{font-size:22px;margin:0 0 4px} .sub{color:var(--mut);margin:0 0 20px;font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
  label{font-weight:600;font-size:13px;display:block;margin-bottom:6px}
  .row{display:flex;gap:10px;align-items:center}
  input[type=text]{flex:1;background:transparent;border:1px solid var(--line);border-radius:9px;
        padding:9px 11px;color:var(--tx);font-size:14px}
  button{background:var(--acc);color:#231400;border:0;border-radius:9px;padding:9px 15px;
        font-weight:700;cursor:pointer;font-size:14px}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--tx);font-weight:600}
  button:disabled{opacity:.5;cursor:default}
  .clips{margin-top:6px} .clip{display:flex;gap:8px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
  .clip .nm{width:180px;font-size:12px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .clip input.tx{flex:1} .clip select,.clip .ord{background:transparent;color:var(--tx);border:1px solid var(--line);border-radius:7px;padding:5px}
  .mut{color:var(--mut);font-size:13px}
  pre{background:#0b0d11;color:#cfe3d0;border-radius:10px;padding:14px;max-height:340px;overflow:auto;font-size:12px;white-space:pre-wrap}
  @media (prefers-color-scheme:light){pre{background:#0f1115}}
  .browser{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:9}
  .browser .inner{background:var(--card);border:1px solid var(--line);border-radius:14px;width:min(640px,92vw);max-height:82vh;display:flex;flex-direction:column}
  .browser header{padding:14px 16px;border-bottom:1px solid var(--line);font-weight:700}
  .browser .cur{padding:8px 16px;color:var(--mut);font-size:12px;word-break:break-all}
  .browser ul{list-style:none;margin:0;padding:0;overflow:auto;flex:1}
  .browser li{padding:10px 16px;cursor:pointer;border-top:1px solid var(--line)}
  .browser li:hover{background:var(--line)} .browser footer{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:10px;justify-content:flex-end}
  .ok{color:var(--ok)} .err{color:var(--err)}
  .badge{display:inline-block;background:var(--acc2);color:#fff;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700}
</style></head><body><div class="wrap">
  <h1>🎬 Автомонтаж</h1>
  <p class="sub">Склейка клипов → вырез пауз → пословные субтитры. Результат называется автоматически: <b>video1, video2, …</b></p>

  <div class="card">
    <label>Папка-источник (откуда брать клипы)</label>
    <div class="row"><input type="text" id="inDir" placeholder="выбери папку с клипами">
      <button class="ghost" onclick="openBrowser('in')">Обзор…</button></div>
    <div id="scanInfo" class="mut" style="margin-top:8px"></div>
    <div class="clips" id="clips"></div>
  </div>

  <div class="card">
    <label>Папка-назначение (куда класть готовый ролик)</label>
    <div class="row"><input type="text" id="outDir" placeholder="выбери папку для результата">
      <button class="ghost" onclick="openBrowser('out')">Обзор…</button></div>
    <div class="mut" style="margin-top:8px">Следующее имя: <span class="badge" id="nextName">video1.mp4</span></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div><label style="margin:0">Модель распознавания</label>
        <select id="model" class="ord" style="margin-top:6px;padding:8px">
          <option value="medium" selected>medium — точнее (по умолчанию)</option>
          <option value="small">small — быстрее</option>
          <option value="large-v3">large-v3 — максимум качества</option>
        </select></div>
      <button id="runBtn" onclick="run()" style="padding:12px 22px">▶ Смонтировать</button>
    </div>
  </div>

  <div class="card" id="logCard" style="display:none">
    <label>Лог</label><pre id="log"></pre>
    <div id="result" style="margin-top:10px"></div>
  </div>
</div>

<div class="browser" id="browser"><div class="inner">
  <header>Выбор папки</header>
  <div class="cur" id="curPath"></div>
  <ul id="dirList"></ul>
  <footer>
    <button class="ghost" onclick="closeBrowser()">Отмена</button>
    <button onclick="chooseHere()">Выбрать эту папку</button>
  </footer>
</div></div>

<script>
let browseFor=null, curPath="{{ start }}", clips=[];
const $=id=>document.getElementById(id);

function openBrowser(which){browseFor=which; $('browser').style.display='flex'; browse(curPath);}
function closeBrowser(){$('browser').style.display='none';}
async function browse(p){
  const r=await fetch('/api/browse?path='+encodeURIComponent(p)); const d=await r.json();
  curPath=d.path; $('curPath').textContent=d.path;
  const ul=$('dirList'); ul.innerHTML='';
  const up=document.createElement('li'); up.textContent='⬆ ..'; up.onclick=()=>browse(d.parent); ul.appendChild(up);
  d.dirs.forEach(name=>{const li=document.createElement('li'); li.textContent='📁 '+name;
    li.onclick=()=>browse(d.path.replace(/\/$/,'')+'/'+name); ul.appendChild(li);});
  if(d.videos.length){const info=document.createElement('li'); info.className='mut';
    info.style.cursor='default'; info.textContent='🎞 видео здесь: '+d.videos.length; ul.appendChild(info);}
}
async function chooseHere(){
  if(browseFor==='in'){$('inDir').value=curPath; await scan();}
  else {$('outDir').value=curPath; await scan();}
  closeBrowser();
}
async function scan(){
  const inD=$('inDir').value, outD=$('outDir').value;
  if(inD){const r=await fetch('/api/scan?path='+encodeURIComponent(inD)); const d=await r.json();
    clips=d.videos.map(f=>({file:f,text:'',trim:'normal',include:true})); renderClips();
    $('scanInfo').textContent='Найдено клипов: '+d.videos.length;}
  if(outD){const r=await fetch('/api/scan?path='+encodeURIComponent(outD)); const d=await r.json();
    $('nextName').textContent=d.next;}
}
function renderClips(){
  const box=$('clips'); box.innerHTML='';
  clips.forEach((c,i)=>{
    const row=document.createElement('div'); row.className='clip';
    row.innerHTML=`<input type="checkbox" ${c.include?'checked':''} onchange="clips[${i}].include=this.checked">
      <span class="nm" title="${c.file}">${c.file}</span>
      <input class="tx" type="text" placeholder="текст реплики (пусто = авто)" value="${c.text.replace(/"/g,'&quot;')}" oninput="clips[${i}].text=this.value">
      <select onchange="clips[${i}].trim=this.value">
        ${['normal','tight','loose','none'].map(t=>`<option ${c.trim===t?'selected':''}>${t}</option>`).join('')}
      </select>
      <button class="ord" onclick="move(${i},-1)">↑</button>
      <button class="ord" onclick="move(${i},1)">↓</button>`;
    box.appendChild(row);
  });
}
function move(i,d){const j=i+d; if(j<0||j>=clips.length)return;
  [clips[i],clips[j]]=[clips[j],clips[i]]; renderClips();}

async function run(){
  const in_dir=$('inDir').value, out_dir=$('outDir').value;
  if(!in_dir||!out_dir){alert('Выбери обе папки');return;}
  if(!clips.some(c=>c.include)){alert('Нет выбранных клипов');return;}
  $('runBtn').disabled=true; $('logCard').style.display='block'; $('log').textContent='Запуск…\n'; $('result').textContent='';
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({in_dir,out_dir,model:$('model').value,clips})});
  const d=await r.json();
  if(d.error){$('log').textContent=d.error;$('runBtn').disabled=false;return;}
  poll(d.job);
}
async function poll(job){
  const r=await fetch('/api/log?job='+job); const d=await r.json();
  $('log').textContent=d.log; $('log').scrollTop=$('log').scrollHeight;
  if(d.done){
    $('runBtn').disabled=false;
    $('result').innerHTML = d.ok
      ? `<span class="ok">✓ Готово:</span> <b>${d.out_file}</b>`
      : `<span class="err">✗ Ошибка — смотри лог выше.</span>`;
    if(d.ok) scan();
    return;
  }
  setTimeout(()=>poll(job),1000);
}
</script></body></html>"""


if __name__ == "__main__":
    print("→ Открой http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
