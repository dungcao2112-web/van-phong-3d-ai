#!/usr/bin/env python3
"""
Văn Phòng 3D AI Agents & Quản Lý GPU VRAM (RTX 5070 Ti) — Sếp Dũng
Chạy nhẹ trên CPU (0 MB VRAM), theo dõi thời gian thực 100% động:
- NGUYÊN TẮC HIỂN THỊ:
  1. Trạng thái hiện tại (current_task, status_label, speech) CHỈ báo công việc ĐANG CHẠY NGAY LÚC NÀY.
  2. Ngay khi Agent làm xong việc và tiến trình kết thúc -> Chuyển ngay về trạng thái "ĐANG NGHỈ" (idle),
     đẩy toàn bộ công việc cũ xuống mục "Lịch sử công việc đã hoàn thành (logs/history)".
"""

from __future__ import annotations

import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
import psutil

PORT = 7890
HOST = "127.0.0.1"
ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"
DIRECT_DIR = ROOT_DIR / "direct_tasks"
CREATE_NO_WINDOW = 0x08000000

_TELEMETRY_LOCK = threading.Lock()
_DIRECT_LOCK = threading.Lock()
_DIRECT_TASKS: Dict[str, Dict[str, Any]] = {}
_DIRECT_HISTORY: Dict[str, List[str]] = {}
_TELEMETRY_CACHE: Dict[str, Any] = {
    "ok": True,
    "timestamp": "",
    "gpu": {},
    "python_processes": [],
    "agents": [],
    "project_k07": {},
    "claude_sync": {},
    "vram_summary": {},
}


def _run_hidden(cmd: List[str], timeout: int = 8) -> str:
    """Chạy lệnh ngầm tuyệt đối không bật cửa sổ Terminal (Tương thích hoàn toàn với pythonw.exe)."""
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return "--:--:--"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S %d/%m")


def _fmt_hms(ts: float) -> str:
    if ts <= 0:
        return "--:--:--"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _read_tail(path: Path, max_lines: int = 15) -> List[str]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 12288:
                f.seek(size - 12288)
            chunk = f.read().decode("utf-8", errors="replace")
        raw = chunk.splitlines()
        cleaned = [
            line.strip()
            for line in raw
            if line.strip() and "System.Management.Automation.RemoteException" not in line
        ]
        return cleaned[-max_lines:]
    except Exception:
        return []


_VRAM_CACHE: Dict[str, Any] = {"ts": 0.0, "total_used": 0, "map": {}}
_CIT_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "online": False, "engine": "VieNeu-TTS-v3-Turbo (GPU • 48kHz)"}
_PROC_CMD_CACHE: Dict[Tuple[int, float], Tuple[str, str, str, str]] = {}
_TARGET_PROC_NAMES = {
    "python.exe",
    "pythonw.exe",
    "claude.exe",
    "antigravity.exe",
    "agy.exe",
    "codex.exe",
    "ffmpeg.exe",
    "ffprobe.exe",
    "cit-voice-studio.exe",
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "bash.exe",
}
_SHELL_NAMES = {"powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe"}
_SHELL_CMD_RE = re.compile(
    r"(K07|claude|codex|agy|muse|qa_images|tasks\\.*\.output|run_codex|run_agy|stage\.ps1|CLAUDE_CODE_SHELL_LAUNCHER_SCRIPT|snapshot-bash|agent_logs|giong_ghi_chu|voice_hoi)",
    re.IGNORECASE,
)


def _query_gpu_and_processes() -> Dict[str, Any]:
    """Lấy thông số GPU từ nvidia-smi + danh sách tiến trình từ psutil C-API (~25ms, không bật powershell.exe)."""
    gpu_info = {
        "name": "NVIDIA GeForce RTX 5070 Ti",
        "temp_c": 0,
        "power_w": 0.0,
        "power_limit_w": 300.0,
        "vram_used_mb": 0,
        "vram_total_mb": 16303,
        "gpu_util_pct": 0,
        "fan_pct": 0,
    }

    smi_out = _run_hidden(
        [
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,power.draw,power.limit,memory.used,memory.total,utilization.gpu,fan.speed",
            "--format=csv,noheader,nounits",
        ],
        timeout=3,
    )
    if smi_out:
        parts = [x.strip() for x in smi_out.splitlines()[0].split(",")]
        if len(parts) >= 7:
            gpu_info["name"] = parts[0]
            try:
                gpu_info["temp_c"] = int(float(parts[1]))
            except Exception:
                pass
            try:
                gpu_info["power_w"] = round(float(parts[2]), 1)
            except Exception:
                pass
            try:
                gpu_info["power_limit_w"] = round(float(parts[3]), 1)
            except Exception:
                pass
            try:
                gpu_info["vram_used_mb"] = int(float(parts[4]))
            except Exception:
                pass
            try:
                gpu_info["vram_total_mb"] = int(float(parts[5]))
            except Exception:
                pass
            try:
                gpu_info["gpu_util_pct"] = int(float(parts[6]))
            except Exception:
                pass
            if len(parts) >= 8:
                try:
                    gpu_info["fan_pct"] = int(float(parts[7]))
                except Exception:
                    pass

    now = time.time()
    need_vram_counter = (
        (now - _VRAM_CACHE["ts"]) > 120.0
        or abs(gpu_info["vram_used_mb"] - _VRAM_CACHE["total_used"]) > 200
    )

    if need_vram_counter:
        ps_vram = r"""
$vramMap = @{}
try {
    $samples = Get-Counter "\GPU Process Memory(*)\Dedicated Usage" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CounterSamples
    foreach ($s in $samples) {
        if ($s.CookedValue -gt 1048576 -and $s.InstanceName -match 'pid_(\d+)_') {
            $pidKey = [string]$Matches[1]
            $mb = [math]::Round($s.CookedValue / 1MB, 1)
            if (-not $vramMap.ContainsKey($pidKey) -or $vramMap[$pidKey] -lt $mb) {
                $vramMap[$pidKey] = $mb
            }
        }
    }
} catch {}
$vramMap | ConvertTo-Json -Compress
"""
        raw_v = _run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_vram],
            timeout=5,
        )
        new_map: Dict[int, float] = {}
        if raw_v and "{" in raw_v:
            try:
                d = json.loads(raw_v[raw_v.find("{") :])
                for k, val in d.items():
                    new_map[int(k)] = float(val)
            except Exception:
                pass
        _VRAM_CACHE["ts"] = now
        _VRAM_CACHE["total_used"] = gpu_info["vram_used_mb"]
        if new_map:
            _VRAM_CACHE["map"] = new_map

    vram_map: Dict[int, float] = _VRAM_CACHE["map"]
    procs_list: List[Dict[str, Any]] = []
    alive_keys = set()

    for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        try:
            info = p.info
            nm = str(info.get("name") or "")
            nm_low = nm.lower()
            pid_num = int(info.get("pid") or 0)
            if nm_low not in _TARGET_PROC_NAMES and pid_num not in vram_map:
                continue
            ctime = float(info.get("create_time") or 0.0)
            key = (pid_num, ctime)
            alive_keys.add(key)
            if key not in _PROC_CMD_CACHE:
                try:
                    cmdline_list = p.cmdline()
                    cmd_s = subprocess.list2cmdline(cmdline_list) if cmdline_list else ""
                except Exception:
                    cmd_s = ""
                try:
                    exe_s = p.exe()
                except Exception:
                    exe_s = ""
                try:
                    cwd_s = p.cwd()
                except Exception:
                    cwd_s = ""
                codex_home_s = ""
                if nm_low in ("codex.exe", "powershell.exe", "pwsh.exe", "agy.exe"):
                    try:
                        env_d = p.environ()
                        codex_home_s = str(env_d.get("CODEX_HOME") or env_d.get("AGY_ACCOUNT") or "")
                    except Exception:
                        codex_home_s = ""
                _PROC_CMD_CACHE[key] = (cmd_s, exe_s, cwd_s, codex_home_s)
            cmd_str, exe_str, cwd_str, codex_home_str = _PROC_CMD_CACHE[key]
            if nm_low in _SHELL_NAMES and not _SHELL_CMD_RE.search(cmd_str):
                continue
            try:
                ram_mb = round(p.memory_info().rss / (1024 * 1024), 1)
            except Exception:
                ram_mb = 0.0
            procs_list.append(
                {
                    "pid": pid_num,
                    "ppid": int(info.get("ppid") or 0),
                    "name": nm,
                    "cmd": cmd_str,
                    "exe": exe_str,
                    "cwd": cwd_str,
                    "codex_home": codex_home_str,
                    "create_time": ctime,
                    "ram_mb": ram_mb,
                    "vram_mb": vram_map.get(pid_num, 0.0),
                }
            )
        except Exception:
            continue

    if len(_PROC_CMD_CACHE) > 250:
        for k in list(_PROC_CMD_CACHE.keys()):
            if k not in alive_keys:
                del _PROC_CMD_CACHE[k]

    return {"gpu": gpu_info, "procs": procs_list}


def _classify_python_process(p: Dict[str, Any], all_procs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Phân loại từng tiến trình Python đang chạy trên máy."""
    cmd = (p.get("cmd") or "").strip()
    exe = (p.get("exe") or "").strip()
    pid = p.get("pid", 0)
    ppid = p.get("ppid", 0)
    vram_mb = float(p.get("vram_mb") or 0.0)
    ram_mb = float(p.get("ram_mb") or 0.0)

    child_vram = sum(
        float(c.get("vram_mb") or 0.0)
        for c in all_procs
        if c.get("ppid") == pid and c.get("pid") != pid
    )

    role = "Python Script"
    agent_id = "system"
    task_desc = cmd or exe or "Tiến trình Python"
    status = "running"
    safe_to_kill = False
    kill_note = "Kiểm tra kỹ trước khi tắt"
    category = "other"

    name_lower = str(p.get("name", "")).lower()
    cmd_lower = cmd.lower()
    if "cit-voice-studio" in name_lower or "cit voice studio" in cmd_lower or "cit-voice-studio" in cmd_lower:
        role = "CIT Voice Studio (VieNeu-TTS-v3-Turbo GPU • Port 8001)"
        agent_id = "cit_voice"
        category = "cit_voice"
        task_desc = f"Đang chạy CIT Voice Studio API (Port 8001, 48kHz) — Chiếm {vram_mb:,.0f} MB VRAM trên RTX 5070 Ti"
        status = "standby"
        safe_to_kill = True
        kill_note = "Có thể đóng CIT Voice Studio để giải phóng ~1.35 GB VRAM nếu đã thu xong giọng"
    elif "omnivoice" in cmd_lower or "studio_server.py" in cmd_lower:
        role = "OmniVoice Studio Server (Cũ - Port 7862)"
        agent_id = "cit_voice"
        category = "omnivoice"
        if vram_mb > 100:
            task_desc = f"Đang nạp model OmniVoice cũ trên CUDA (Chiếm {vram_mb:,.0f} MB VRAM)"
            status = "vram_hog"
        else:
            task_desc = f"Tiến trình quản lý OmniVoice Launcher (Tiến trình con chiếm {child_vram:,.0f} MB VRAM)"
            status = "vram_hog" if child_vram > 100 else "idle"
        safe_to_kill = True
        kill_note = "An toàn để tắt (Giải phóng ~8.8 GB VRAM)"
    elif "muse_batch.py" in cmd_lower:
        m = re.search(r"muse_batch\.py\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(\d+))?", cmd)
        if m:
            mu_num, s0, s1 = m.group(1), m.group(2), m.group(3)
            bsize = m.group(4) or "12"
            role = f"Muse {mu_num} — Vẽ tranh Ghibli hàng loạt"
            agent_id = f"muse{mu_num}"
            category = "muse"
            task_desc = f"Đang chạy lô vẽ ảnh: Shot S{int(s0):03d} → S{int(s1):03d} (Cỡ lô {bsize} ảnh)"
        else:
            role = "Muse Batch Worker"
            agent_id = "muse1"
            category = "muse"
            task_desc = "Đang điều khiển trình duyệt GPM vẽ ảnh Muse AI"
        status = "working"
        safe_to_kill = False
        kill_note = "⚠️ ĐANG VẼ TRANH! Không nên tắt kẻo đứt quãng lô ảnh đang vẽ."
    elif "muse.py" in cmd_lower or "muse_send_video.py" in cmd_lower:
        is_grab = "--grab" in cmd_lower
        is_check = "--check" in cmd_lower
        m_out = re.search(r"out_k(\d+)_(\d+)_(\d+)", cmd_lower)
        mu_num = m_out.group(1) if m_out else ("2" if "k2_" in cmd_lower else "1")
        role = f"Muse {mu_num} — CDP Browser Worker"
        agent_id = f"muse{mu_num}"
        category = "muse"
        if is_grab:
            task_desc = f"Đang tải ảnh từ Muse AI về máy (--grab) cho Muse {mu_num}"
        elif is_check:
            task_desc = f"Đang kiểm tra kết nối trình duyệt GPM Profile Muse {mu_num} (--check)"
        else:
            task_desc = f"Đang gửi prompt và ảnh tham chiếu lên Muse {mu_num} qua CDP"
        status = "working"
        safe_to_kill = False
        kill_note = "⚠️ ĐANG TƯƠNG TÁC TRÌNH DUYỆT MUSE! Không tắt lúc này."
    elif "make_voice" in cmd_lower or "voice_hoi" in cmd_lower or "kiem_ghi_chu" in cmd_lower:
        role = "CIT Voice Worker — Thu giọng từng hồi (Giọng Kênh Phật Pháp)"
        agent_id = "cit_voice"
        category = "cit_voice_worker"
        task_desc = f"Đang gọi CIT Voice Studio (Port 8001) thu giọng từng hồi: {cmd[:140]}"
        status = "working"
        safe_to_kill = False
        kill_note = "⚠️ Đang thu âm giọng đọc CIT Voice"
    elif "build_video.py" in cmd_lower or "run_channel.py" in cmd_lower or "build_audio.py" in cmd_lower:
        role = "K07 Video Render Worker"
        agent_id = "agy"
        category = "k07_pipeline"
        task_desc = f"Đang kết xuất video K07: {cmd[:140]}"
        status = "working"
        safe_to_kill = False
        kill_note = "⚠️ Đang dựng video K07"
    elif "yt_dlp" in cmd_lower:
        role = "Agy — YouTube Subtitle Downloader (yt-dlp)"
        agent_id = "agy"
        category = "k07_pipeline"
        task_desc = f"Đang tải phụ đề video mẫu khảo sát: {cmd[:140]}"
        status = "working"
        safe_to_kill = False
        kill_note = "⚠️ Agy đang tải phụ đề khảo sát kịch bản"
    elif "mcp-for-blender" in cmd_lower:
        role = "MCP Server for Blender 3D"
        agent_id = "blender_mcp"
        category = "mcp"
        task_desc = "Cầu nối điều khiển Blender 3D cho Agent (0 MB VRAM)"
        status = "standby"
        safe_to_kill = True
        kill_note = "Có thể tắt nếu không dùng Blender MCP"
    elif "lan-transfer" in cmd_lower:
        role = "LAN Transfer Server"
        agent_id = "lan_server"
        category = "utility"
        task_desc = "Server truyền file nội bộ qua mạng LAN (E:\\Chuyển Dữ Liệu\\lan-transfer)"
        status = "standby"
        safe_to_kill = True
        kill_note = "Tiện ích chạy nền nhẹ (0 MB VRAM)"
    elif "vanphong3d" in cmd_lower or ("server.py" in cmd_lower and str(PORT) in cmd_lower):
        role = "Văn Phòng 3D Server (Dịch vụ nền độc lập)"
        agent_id = "aux_services"
        category = "self"
        task_desc = f"Server giao diện Văn Phòng 3D (Port {PORT}, 0 MB VRAM)"
        status = "standby"
        safe_to_kill = False
        kill_note = "Đây là server của giao diện Văn Phòng 3D đang mở"
    elif "comfy" in cmd_lower:
        role = "ComfyUI Server"
        agent_id = "comfyui"
        category = "comfyui"
        task_desc = f"Server tạo ảnh/video ComfyUI ({vram_mb:,.0f} MB VRAM)"
        status = "vram_hog" if vram_mb > 500 else "working"
        safe_to_kill = True
        kill_note = "Tắt để giải phóng VRAM nếu không render ComfyUI"

    return {
        "pid": pid,
        "ppid": ppid,
        "name": p.get("name", "python.exe"),
        "role": role,
        "agent_id": agent_id,
        "category": category,
        "task_desc": task_desc,
        "cmd": cmd,
        "exe": exe,
        "ram_mb": ram_mb,
        "vram_mb": vram_mb,
        "effective_vram_mb": max(vram_mb, child_vram),
        "status": status,
        "safe_to_kill": safe_to_kill,
        "kill_note": kill_note,
    }


def _parse_muse_status(
    muse_num: int,
    k07_t02: Path,
    py_procs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Chỉ báo ĐANG VẼ TRANH khi tiến trình muse_batch.py / muse.py thực sự đang chạy!
    Tự động nhận diện thư mục tập/dự án đang vẽ (T02, T02B, T03... hoặc bất kỳ --out dir nào).
    """
    active_procs = [p for p in py_procs if p.get("agent_id") == f"muse{muse_num}"]
    active_pids = [p["pid"] for p in active_procs]
    is_running = len(active_pids) > 0

    ep_dir = k07_t02
    k07_root = k07_t02.parent
    # Nếu đang chạy thì lấy thư mục trực tiếp từ tiến trình muse_batch.py / muse.py
    if active_procs:
        p0 = active_procs[0]
        cmd0 = p0.get("cmd") or ""
        m_out_dir = re.search(r'--out\s+"?([^"]+)"?(?:\s|$)', cmd0)
        m_folder_arg = re.search(r'muse_batch\.py["\']?\s+"?([A-Za-z]:[\\/][^"]+)"?', cmd0)
        if m_out_dir:
            cand = Path(m_out_dir.group(1).strip())
            if cand.name.lower() == "art_raw" and cand.parent.name.lower() == "assets":
                ep_dir = cand.parent.parent
            elif cand.is_dir():
                ep_dir = cand
        elif m_folder_arg:
            cand = Path(m_folder_arg.group(1).strip().rstrip("\\/"))
            if cand.is_dir():
                ep_dir = cand
        elif p0.get("cwd") and Path(p0["cwd"]).is_dir():
            ep_dir = Path(p0["cwd"])
    else:
        # Khi nghỉ: tìm thư mục T* có muse_logs hoặc art_raw mới nhất trong G:\K07
        try:
            best_mt = 0.0
            for td in k07_root.glob("T*"):
                if td.is_dir():
                    ml = td / "muse_logs" / f"muse{muse_num}.log"
                    if ml.is_file() and ml.stat().st_mtime > best_mt:
                        best_mt = ml.stat().st_mtime
                        ep_dir = td
        except Exception:
            pass

    ep_label = _short_project_label(str(ep_dir))
    log_file = ep_dir / "muse_logs" / f"muse{muse_num}.log"
    if not log_file.is_file():
        for alt_log in (ep_dir / "muse_batch.log", ep_dir / "muse.log"):
            if alt_log.is_file():
                log_file = alt_log
                break
    art_dir = ep_dir / "Assets" / "art_raw"
    if not art_dir.is_dir():
        for sub_name in ("anh", "images", "art_raw", "assets/images", "output"):
            cand_sub = ep_dir / sub_name
            if cand_sub.is_dir():
                art_dir = cand_sub
                break
        else:
            if ep_dir.is_dir():
                art_dir = ep_dir
    tail = _read_tail(log_file, 15)

    default_start = 1 if muse_num == 1 else 67
    default_end = 66 if muse_num == 1 else 132
    default_total = default_end - default_start + 1

    done_in_half: List[str] = []
    latest_art_file: Optional[Path] = None
    latest_art_mtime: float = 0.0

    if art_dir.is_dir():
        for i in range(default_start, default_end + 1):
            p = art_dir / f"A{i:03d}.png"
            if p.is_file():
                done_in_half.append(f"A{i:03d}.png")
                try:
                    mt = p.stat().st_mtime
                    if mt > latest_art_mtime:
                        latest_art_mtime = mt
                        latest_art_file = p
                except Exception:
                    pass
        # Hỗ trợ các dự án ngoài K07 (ví dụ Reel Món Ăn, FB AFF... lưu ảnh *.png / *.jpg / *.webp)
        if not done_in_half:
            try:
                generic_imgs = [
                    f for f in art_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                ]
                for gf in generic_imgs:
                    done_in_half.append(gf.name)
                    mt = gf.stat().st_mtime
                    if mt > latest_art_mtime:
                        latest_art_mtime = mt
                        latest_art_file = gf
            except Exception:
                pass

    active_procs = [p for p in py_procs if p.get("agent_id") == f"muse{muse_num}"]
    active_pids = [p["pid"] for p in active_procs]
    is_running = len(active_pids) > 0

    live_cli_range = ""
    is_grabbing = False
    is_custom_project = False
    for p in active_procs:
        c = p.get("cmd") or ""
        m_batch = re.search(r"muse_batch\.py\s+(\d+)\s+(\d+)\s+(\d+)", c)
        if m_batch:
            live_cli_range = f"S{int(m_batch.group(2)):03d} → S{int(m_batch.group(3)):03d}"
        elif "muse_batch.py" in c.lower():
            is_custom_project = True
            live_cli_range = f"[{ep_label}] ({len(done_in_half)} ảnh đã lưu)"
        if "--grab" in c:
            is_grabbing = True

    current_batch = ""
    current_range = live_cli_range or f"S{default_start:03d} → S{default_end:03d}"
    last_action = f"Dự án: {ep_label}" if is_custom_project else "Chưa có log"
    last_xong = ""
    waiting_seconds_left: Optional[int] = None

    for line in reversed(tail):
        if not current_batch:
            m_task = re.search(r"TASK\s+(\d+)\s+(\S+)\s+(S\d+)\s*->\s*(S\d+)", line)
            if m_task:
                current_batch = f"Lô {m_task.group(2)} ({m_task.group(3)} → {m_task.group(4)})"
                if not live_cli_range:
                    current_range = f"{m_task.group(3)} → {m_task.group(4)}"
        if not last_xong:
            m_xong = re.search(r"(\d{2}:\d{2}:\d{2})\s+XONG\s+(\d+)\s+(\d+)", line)
            if m_xong:
                last_xong = f"Hoàn tất lô S{int(m_xong.group(2)):03d}→S{int(m_xong.group(3)):03d} lúc {m_xong.group(1)}"
        if last_action == "Chưa có log" and line:
            last_action = line

    if is_running and tail and "giay truoc khi lay anh" in tail[-1] and log_file.is_file():
        m_wait = re.search(r"ve xong\s+(\d+)\s+giay", tail[-1])
        wait_target = int(m_wait.group(1)) if m_wait else 660
        try:
            elapsed = time.time() - log_file.stat().st_mtime
            rem = int(wait_target - elapsed)
            if rem > 0:
                waiting_seconds_left = rem
        except Exception:
            pass

    pct = round(len(done_in_half) * 100.0 / max(1, default_total), 1)
    log_mtime_str = _fmt_time(log_file.stat().st_mtime) if log_file.is_file() else ""

    if is_running:
        if is_grabbing:
            speech = f"📥 Đang tải ảnh về máy (--grab): {current_batch or current_range}"
            task_summary = f"🎨 ĐANG CHẠY: Tải ảnh từ Muse AI về máy ({current_batch or current_range}) • {last_action}"
        elif waiting_seconds_left is not None:
            mins, secs = divmod(waiting_seconds_left, 60)
            speech = f"🎨 Đang vẽ {current_range} (Còn ~{mins}p{secs:02d}s lấy ảnh)"
            task_summary = f"🎨 ĐANG CHẠY: Đợi Muse AI vẽ {current_batch or current_range} (Còn ~{mins}p{secs:02d}s sẽ tải ảnh về)"
        else:
            speech = f"🎨 Đang vẽ {current_range}"
            task_summary = f"🎨 ĐANG CHẠY: {current_batch or current_range} • {last_action}"
        history_logs = [f"🟢 ĐANG CHẠY TRỰC TIẾP: {task_summary}"] + tail
    else:
        # KHI ĐÃ XONG VIỆC: Chỉ báo trạng thái hiện tại là ĐANG NGHỈ!
        speech = "💤 Đang nghỉ • Chờ lô tranh mới"
        task_summary = "Hiện đang nghỉ — Không có lô tranh nào đang vẽ."
        history_logs = [
            f"🕒 [Lịch sử gần nhất - {log_mtime_str}] Đã hoàn thành {last_xong or current_batch} ({len(done_in_half)}/{default_total} ảnh Tập 2).",
            "--- Nhật ký phiên vẽ trước (Đã kết thúc) ---",
        ] + tail[-10:]

    return {
        "is_running": is_running,
        "pids": active_pids,
        "done_count": len(done_in_half),
        "total_count": default_total,
        "progress_pct": pct if is_running else None,  # Chỉ hiện thanh tiến độ khi đang chạy!
        "speech": speech,
        "task_summary": task_summary,
        "log_tail": history_logs,
        "latest_art_name": latest_art_file.name if latest_art_file else None,
        "latest_art_url": f"/api/latest-art?muse={muse_num}&t={int(latest_art_mtime)}" if latest_art_file else None,
    }


def _short_project_label(dir_str: str) -> str:
    """Tạo nhãn tên thư mục dự án ngắn gọn từ bất kỳ đường dẫn nào trên máy."""
    if not dir_str:
        return "G:\\K07"
    p = Path(dir_str)
    low = dir_str.lower()
    if "k07" in low:
        m_ep = re.search(r"\\(t\d+[a-z]?)(?:\\|$)", low)
        if m_ep:
            return f"K07\\{m_ep.group(1).upper()}"
        return "K07"
    parts = [x for x in p.parts if x and x not in ("G:\\", "D:\\", "E:\\", "C:\\", "YouTube 2025", "NAO-Dũng - 2026-09-10")]
    if len(parts) >= 2:
        tail = f"{parts[-2]}\\{parts[-1]}"
    elif parts:
        tail = parts[-1]
    else:
        tail = p.name or dir_str
    return tail[:34] + ("..." if len(tail) > 34 else "")


def _extract_agy_brief_title(cmd_str: str, cwd_str: str = "") -> str:
    """Trích xuất tiêu đề công việc thực tế từ tham số -p của agy.exe hoặc từ .agent_logs."""
    proj_lbl = _short_project_label(cwd_str)
    m = re.search(r'-p\s+"([^"]+)"', cmd_str)
    if m:
        lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
        if lines:
            first_line = re.sub(r"^#+\s*", "", lines[0]).strip()
            return f"[{proj_lbl}] {first_line[:115]}"
    m2 = re.search(r"#\s*([^\r\n]+)", cmd_str)
    if m2:
        return f"[{proj_lbl}] {m2.group(1).strip()[:115]}"
    return f"[{proj_lbl}] Đang thực thi lệnh tự động từ CEO Claude"


def _extract_codex_log_info(log_path: Optional[Path], fallback_dir: str = "") -> Tuple[str, str]:
    """
    Đọc cả file .agent_logs/codex_*.txt (ở BẤT KỲ thư mục dự án nào) lẫn logs/codex*.log:
    Trả về (tiêu_đề_kèm_thư_mục, gợi_ý_agent_id: 'codex1'|'codex2'|'codex3'|'').
    """
    proj_lbl = _short_project_label(fallback_dir)
    if not log_path or not log_path.is_file():
        return (f"[{proj_lbl}] Đang thực thi Brief từ CEO Claude", "")
    try:
        with log_path.open("rb") as f:
            head_txt = f.read(4096).decode("utf-8", errors="replace")
        lines = head_txt.splitlines()
        for ln in lines[:12]:
            if ln.lower().startswith("workdir:"):
                wd = ln.split(":", 1)[1].strip()
                if wd:
                    proj_lbl = _short_project_label(wd)
                break

        hint_agent = ""
        if re.search(r"\bCodex\s*1\b", head_txt, re.IGNORECASE):
            hint_agent = "codex1"
        elif re.search(r"\bCodex\s*2\b", head_txt, re.IGNORECASE):
            hint_agent = "codex2"
        elif re.search(r"\bCodex\s*3\b", head_txt, re.IGNORECASE):
            hint_agent = "codex3"

        # Nếu là định dạng .agent_logs/codex_*.txt thì prompt nằm ngay sau dòng "user"
        user_idx = -1
        for idx, ln in enumerate(lines[:25]):
            if ln.strip() == "user":
                user_idx = idx
                break
        search_lines = lines[user_idx + 1 : user_idx + 12] if user_idx >= 0 else lines[:30]
        for ln in search_lines:
            s = ln.strip().lstrip("\ufeff").strip()
            if not s or s.startswith("--------") or s.lower().startswith("openai codex"):
                continue
            title = re.sub(r"^#+\s*", "", s).strip()
            title = re.sub(r"^Codex\s*\d*\s*:\s*", "", title, flags=re.IGNORECASE).strip()
            title = re.sub(r"^MỤC TIÊU\s*:\s*", "", title, flags=re.IGNORECASE).strip()
            if title:
                return (f"[{proj_lbl}] {title[:115]}", hint_agent)
    except Exception:
        pass
    return (f"[{proj_lbl}] {log_path.name}", "")


def _extract_codex_log_title(log_path: Optional[Path]) -> str:
    return _extract_codex_log_info(log_path, str(log_path.parent.parent if log_path else ""))[0]


def _inspect_claude_sync_and_k07_agents(
    k07_root: Path, all_procs: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Hoạt động ĐA THƯ MỤC (Không giới hạn trong G:\\K07):
    - Tự động phát hiện mọi tiến trình Codex / Agy / Muse / FFmpeg / Shell của Claude ở BẤT KỲ thư mục dự án nào trên máy.
    - Nếu Claude giao 2–3 việc Codex chạy song song cùng lúc (kể cả ở nhiều thư mục khác nhau), tự động phân bổ cho
      cả Codex 1, Codex 2, Codex 3 cùng ngồi vào bàn làm việc để phản ánh đúng 100% thực tế!
    """
    now = time.time()

    # 1. Kiểm tra các tiến trình thực tế đang sống trên hệ điều hành (trên TOÀN BỘ các thư mục)
    agy_cli_procs = [
        p
        for p in all_procs
        if str(p.get("name", "")).lower() in ("agy.exe", "agy")
        or "run_agy.ps1" in (p.get("cmd") or "").lower()
    ]
    # Chỉ lấy tiến trình codex.exe thực thi task ("exec "), bỏ qua codex sandbox / app-server / exec-server / mxc
    codex_exec_procs = [
        p
        for p in all_procs
        if str(p.get("name", "")).lower() == "codex.exe"
        and re.search(r"\bexec\b", (p.get("cmd") or ""), re.IGNORECASE)
        and "exec-server" not in (p.get("cmd") or "").lower()
        and "app-server" not in (p.get("cmd") or "").lower()
    ]
    ffmpeg_procs = [
        p
        for p in all_procs
        if str(p.get("name", "")).lower() in ("ffmpeg.exe", "ffprobe.exe")
        or "build_video.py" in (p.get("cmd") or "").lower()
        or "run_channel.py" in (p.get("cmd") or "").lower()
    ]
    muse_batch_procs = [
        p
        for p in all_procs
        if "muse_batch.py" in (p.get("cmd") or "").lower() or "muse.py" in (p.get("cmd") or "").lower()
    ]

    # Các tiến trình Shell ngầm do Claude Code 2.1.308 bật lên (PowerShell launcher hoặc Git Bash)
    claude_shell_procs = [
        p
        for p in all_procs
        if (
            "claude_code_shell_launcher_script" in (p.get("cmd") or "").lower()
            or "snapshot-bash" in (p.get("cmd") or "").lower()
        )
    ]

    # 2. Thu thập tất cả các thư mục dự án đang hoạt động trên máy (từ CWD của Claude, Codex, Agy, Shell + G:\K07)
    project_dirs: List[Path] = []
    seen_dirs = set()

    def _add_proj_dir(d_str: str) -> None:
        if not d_str:
            return
        try:
            dp = Path(d_str)
            # Nếu đang ở thư mục con T02/T03 thì thêm cả thư mục cha lẫn chính nó
            for cand in (dp, dp.parent):
                key_s = str(cand).lower().rstrip("\\/")
                if key_s and key_s not in seen_dirs and cand.is_dir():
                    seen_dirs.add(key_s)
                    project_dirs.append(cand)
        except Exception:
            pass

    _add_proj_dir(str(k07_root))
    for p in all_procs:
        _add_proj_dir(p.get("cwd") or "")
        cmd_s = p.get("cmd") or ""
        m_c = re.search(r'-C\s+"?([^"]+)"?\s+-c', cmd_s)
        if m_c:
            _add_proj_dir(m_c.group(1).strip())

    # Quét toàn bộ file log trong <project_dir>/.agent_logs và G:\K07\logs
    all_codex_logs: List[Path] = []
    all_agy_logs: List[Path] = []
    for pdir in project_dirs:
        ag_log_dir = pdir / ".agent_logs"
        if ag_log_dir.is_dir():
            try:
                all_codex_logs.extend(ag_log_dir.glob("codex_*.txt"))
                all_agy_logs.extend(ag_log_dir.glob("agy_*.txt"))
            except Exception:
                pass
        legacy_log_dir = pdir / "logs"
        if legacy_log_dir.is_dir():
            try:
                all_codex_logs.extend(legacy_log_dir.glob("codex*.log"))
                all_agy_logs.extend(legacy_log_dir.glob("agy*.log"))
            except Exception:
                pass

    # Khử trùng lặp và sắp xếp mới nhất lên đầu
    def _dedup_sort_logs(paths: List[Path], limit: int = 12) -> List[Path]:
        uniq: Dict[str, Path] = {}
        for lp in paths:
            try:
                uniq[str(lp.resolve()).lower()] = lp
            except Exception:
                uniq[str(lp).lower()] = lp
        return sorted(uniq.values(), key=lambda x: x.stat().st_mtime if x.is_file() else 0.0, reverse=True)[:limit]

    all_codex_logs = _dedup_sort_logs(all_codex_logs, 12)
    all_agy_logs = _dedup_sort_logs(all_agy_logs, 10)

    # Nếu Agy vừa được gọi qua run_agy.ps1 (file agy_*.txt mới cập nhật trong 25 giây qua và có claude_shell_procs)
    if not agy_cli_procs and all_agy_logs and claude_shell_procs:
        try:
            if (now - all_agy_logs[0].stat().st_mtime) < 25:
                agy_cli_procs = [claude_shell_procs[0]]
        except Exception:
            pass

    # 3. Ghép từng tiến trình codex.exe exec đang chạy với file log & phân bổ vào bàn Codex 1 / Codex 2 / Codex 3
    codex_slots: Dict[str, Optional[Dict[str, Any]]] = {"codex1": None, "codex2": None, "codex3": None}
    used_log_paths = set()

    for proc in sorted(codex_exec_procs, key=lambda x: float(x.get("create_time") or 0.0)):
        p_cwd = proc.get("cwd") or ""
        cmd_s = proc.get("cmd") or ""
        m_c = re.search(r'-C\s+"?([^"]+)"?\s+-c', cmd_s)
        workdir = m_c.group(1).strip() if m_c else p_cwd
        p_ctime = float(proc.get("create_time") or now)

        # Tìm file log khớp nhất trong <workdir>/.agent_logs hoặc all_codex_logs
        matched_log: Optional[Path] = None
        best_diff = 999999.0
        for lp in all_codex_logs:
            if str(lp) in used_log_paths:
                continue
            try:
                # Ưu tiên log nằm cùng thư mục workdir và được tạo gần thời điểm create_time của proc
                same_dir = workdir and (workdir.lower() in str(lp).lower())
                ctime_diff = abs(lp.stat().st_ctime - p_ctime)
                score = ctime_diff - (300.0 if same_dir else 0.0)
                if score < best_diff:
                    best_diff = score
                    matched_log = lp
            except Exception:
                pass

        if matched_log:
            used_log_paths.add(str(matched_log))
        title_str, hint_ag = _extract_codex_log_info(matched_log, workdir)

        chome = (proc.get("codex_home") or "").lower()
        if hint_ag in ("codex1", "codex2", "codex3"):
            pref = hint_ag
        elif "codex-account3" in chome or "codex2" in cmd_s.lower():
            pref = "codex2"
        elif "codex-account2" in chome or "codex3" in cmd_s.lower():
            pref = "codex3"
        else:
            pref = "codex1"

        # Nếu bàn ưu tiên đã có tiến trình Codex khác đang ngồi làm việc song song -> chuyển sang bàn Codex còn trống!
        target_slot = pref
        if codex_slots[target_slot] is not None:
            for alt in ("codex1", "codex2", "codex3"):
                if codex_slots[alt] is None:
                    target_slot = alt
                    break

        if codex_slots[target_slot] is None:
            codex_slots[target_slot] = {
                "proc": proc,
                "pid": proc["pid"],
                "ram_mb": float(proc.get("ram_mb") or 0.0),
                "workdir": workdir,
                "log_path": matched_log,
                "title": title_str,
            }
        else:
            # Trường hợp chạy > 3 tiến trình Codex cùng lúc: gộp thêm vào slot
            codex_slots[target_slot]["title"] += f" + {title_str}"

    # 4. Tìm các task ngầm của Claude Code (tasks/*.output trên TOÀN BỘ các phiên Claude)
    claude_temp_root = Path(r"C:\Users\Dung\AppData\Local\Temp\claude")
    task_files: List[Path] = []
    if claude_temp_root.is_dir():
        try:
            task_files = sorted(
                claude_temp_root.glob("**/tasks/*.output"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:10]
        except Exception:
            task_files = []

    claude_tasks_list: List[Dict[str, Any]] = []
    # Trước tiên, liệt kê TẤT CẢ các tác vụ đang chạy thực tế trên hệ điều hành để bảng điều phối Claude luôn chính xác 100%
    for slot_id, slot_label in (("codex1", "Codex 1"), ("codex2", "Codex 2"), ("codex3", "Codex 3")):
        sinfo = codex_slots[slot_id]
        if sinfo:
            claude_tasks_list.append(
                {
                    "task_id": f"pid{sinfo['pid']}",
                    "agent": slot_label,
                    "label": f"{slot_label} — {sinfo['title'][:110]}",
                    "is_running": True,
                    "notified_claude": False,
                    "exit_code": None,
                    "time_str": _fmt_hms(float(sinfo["proc"].get("create_time") or now)),
                    "last_line": f"Đang chạy tại: {_short_project_label(sinfo['workdir'])}",
                }
            )

    for ap in agy_cli_procs:
        atitle = _extract_agy_brief_title(ap.get("cmd") or "", ap.get("cwd") or "")
        claude_tasks_list.append(
            {
                "task_id": f"pid{ap['pid']}",
                "agent": "Agy",
                "label": f"Agy — {atitle[:110]}",
                "is_running": True,
                "notified_claude": False,
                "exit_code": None,
                "time_str": _fmt_hms(float(ap.get("create_time") or now)),
                "last_line": atitle,
            }
        )

    # Bổ sung lịch sử các task đã xong từ tasks/*.output
    for tf in task_files[:10]:
        tid = tf.stem
        try:
            mt = tf.stat().st_mtime
            raw_txt = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        has_exit_marker = "[exited with code" in raw_txt or "=== AGENT RUN:" in raw_txt
        if not has_exit_marker:
            continue
        m_code = re.search(r"(?:\[exited with code\s+(\d+)\]|exit:\s*(\d+))", raw_txt)
        exit_code = int(m_code.group(1) or m_code.group(2)) if m_code else 0
        proj_folder = tf.parent.parent.parent.name
        short_pf = "K07" if "YOUTUBE" in proj_folder.upper() else "Dự án"
        if "AGENT RUN: codex" in raw_txt or "codex" in raw_txt.lower():
            label = f"Codex [{short_pf}] — Hoàn tất Brief"
        elif "AGENT RUN: agy" in raw_txt or "agy" in raw_txt.lower():
            label = f"Agy [{short_pf}] — Hoàn tất Kỹ thuật"
        elif "TASK" in raw_txt and "Muse" in raw_txt:
            label = f"Muse [{short_pf}] — Hoàn tất lô vẽ ảnh"
        else:
            label = f"Tác vụ #{tid[:7]} [{short_pf}]"
        claude_tasks_list.append(
            {
                "task_id": tid,
                "agent": "Worker",
                "label": label,
                "is_running": False,
                "notified_claude": True,
                "exit_code": exit_code,
                "time_str": _fmt_hms(mt),
                "last_line": "Đã hoàn thành & báo về CEO Claude",
            }
        )

    # Xây dựng lịch sử chung cho Codex từ all_codex_logs
    codex_history_all: List[str] = []
    for lp in all_codex_logs[:8]:
        if str(lp) in used_log_paths:
            continue
        try:
            t_str, _ = _extract_codex_log_info(lp, str(lp.parent.parent))
            codex_history_all.append(f"🕒 [{_fmt_time(lp.stat().st_mtime)}] Đã xong: {t_str} ({lp.name})")
        except Exception:
            pass

    def _build_codex_agent_state(slot_id: str, icon: str, idle_desc: str) -> Dict[str, Any]:
        sinfo = codex_slots[slot_id]
        if sinfo:
            lp = sinfo["log_path"]
            log_name = lp.name if lp else f"PID {sinfo['pid']}"
            tail_lines = _read_tail(lp, 8) if lp else []
            return {
                "status": "working",
                "status_label": "ĐANG LÀM VIỆC",
                "pids": [sinfo["pid"]],
                "ram_mb": round(sinfo["ram_mb"], 1),
                "current_task": f"{icon} ĐANG CHẠY (PID {sinfo['pid']}): {sinfo['title']}",
                "speech": f"{icon} {sinfo['title'][:58]}...",
                "logs": [
                    f"🟢 ĐANG CHẠY TRỰC TIẾP ({log_name}): {sinfo['title']}",
                    f"📂 Thư mục dự án: {sinfo['workdir']}",
                    "--- Log trực tiếp từ tiến trình ---",
                ]
                + tail_lines
                + ["--- Lịch sử các việc đã xong trên toàn bộ các thư mục ---"]
                + codex_history_all[:6],
            }
        return {
            "status": "idle",
            "status_label": "ĐANG NGHỈ",
            "pids": [],
            "ram_mb": 0.0,
            "current_task": idle_desc,
            "speech": "💤 Đang nghỉ • Sẵn sàng nhận việc",
            "logs": ["💤 Tình trạng hiện tại: Đang nghỉ (Không có tiến trình chạy)."] + codex_history_all[:6],
        }

    c1_state = _build_codex_agent_state("codex1", "🔍", "Hiện đang nghỉ — Không có công việc nào đang chạy.")
    c2_state = _build_codex_agent_state("codex2", "💻", "Hiện đang nghỉ — Không có công việc nào đang chạy.")
    c3_state = _build_codex_agent_state("codex3", "⚡", "Hiện đang nghỉ — Sẵn sàng nhận việc tăng cường / song song.")

    # --- TRẠNG THÁI AGY (ANTIGRAVITY) TRÊN TOÀN BỘ THƯ MỤC ---
    agy_history: List[str] = []
    for alp in all_agy_logs[:6]:
        try:
            proj_lbl = _short_project_label(str(alp.parent.parent))
            agy_history.append(f"🕒 [{_fmt_time(alp.stat().st_mtime)}] [{proj_lbl}] Hoàn thành log: {alp.name}")
        except Exception:
            pass

    if agy_cli_procs:
        ap = agy_cli_procs[0]
        brief_title = _extract_agy_brief_title(ap.get("cmd") or "", ap.get("cwd") or "")
        if all_agy_logs and "Đang thực thi lệnh" in brief_title:
            tail_a = _read_tail(all_agy_logs[0], 4)
            if tail_a:
                brief_title = f"[{_short_project_label(str(all_agy_logs[0].parent.parent))}] {tail_a[-1][:110]}"
        agy_pids = [p["pid"] for p in agy_cli_procs]
        agy_status = "working"
        agy_badge = "ĐANG LÀM VIỆC"
        agy_task = f"⚡ ĐANG CHẠY (PID {agy_pids[0]}): {brief_title}"
        agy_speech = f"⚡ {brief_title[:58]}..."
        agy_logs_out = [
            f"🟢 ĐANG CHẠY TRỰC TIẾP (PID {agy_pids}): {brief_title}",
            "--- Lịch sử hoạt động gần nhất ---",
        ] + agy_history
    elif ffmpeg_procs:
        f_pids = [p["pid"] for p in ffmpeg_procs]
        f_cwd = _short_project_label(ffmpeg_procs[0].get("cwd") or "")
        agy_status = "working"
        agy_badge = "ĐANG DỰNG VIDEO"
        agy_task = f"🎬 ĐANG CHẠY (PID {f_pids}): Kết xuất video / xử lý FFmpeg tại [{f_cwd}]"
        agy_speech = f"🎬 Đang dựng video [{f_cwd}]..."
        agy_logs_out = [f"🟢 ĐANG CHẠY RENDER VIDEO (PID {f_pids}) tại [{f_cwd}]"] + agy_history
    else:
        agy_status = "idle"
        agy_badge = "ĐANG NGHỈ"
        agy_task = "Hiện đang nghỉ — Không có tác vụ ngầm nào đang chạy (Đang trực trò chuyện cùng Sếp Dũng)."
        agy_speech = "💤 Đang nghỉ • Sẵn sàng nhận lệnh"
        agy_logs_out = [
            "💤 Tình trạng hiện tại: Không có tiến trình ngầm nào đang chạy.",
            "--- Lịch sử công việc đã hoàn thành gần đây ---",
        ] + agy_history

    # --- TRẠNG THÁI CEO CLAUDE ---
    running_claude_tasks = [t for t in claude_tasks_list if t["is_running"]]
    if running_claude_tasks:
        labels_joined = " | ".join(t["label"] for t in running_claude_tasks[:3])
        claude_status = "working"
        claude_badge = f"ĐIỀU PHỐI ({len(running_claude_tasks)} VIỆC)"
        claude_task_desc = (
            f"📋 ĐANG ĐIỀU PHỐI {len(running_claude_tasks)} TÁC VỤ SONG SONG: {labels_joined}"
        )
        claude_speech = f"📋 Đang chạy {len(running_claude_tasks)} việc: {running_claude_tasks[0]['label'][:44]}..."
    elif claude_shell_procs:
        sh_cwd = _short_project_label(claude_shell_procs[0].get("cwd") or "")
        claude_status = "working"
        claude_badge = "ĐANG GIÁM SÁT"
        claude_task_desc = f"📋 Đang chạy {len(claude_shell_procs)} tiến trình giám sát ngầm tại [{sh_cwd}]."
        claude_speech = f"📋 Đang giám sát tiến trình tại [{sh_cwd}]"
    else:
        claude_status = "standby"
        claude_badge = "CHỜ LỆNH MỚI"
        claude_task_desc = "Hiện đang nghỉ chờ lệnh — Không có tác vụ ngầm nào đang chạy."
        claude_speech = "💤 Chờ lệnh điều phối mới"

    return {
        "claude_sync": {
            "active_tasks_count": max(len(running_claude_tasks), len(claude_shell_procs)),
            "recent_tasks": claude_tasks_list[:8],
        },
        "codex1": c1_state,
        "codex2": c2_state,
        "codex3": c3_state,
        "agy": {
            "status": agy_status,
            "status_label": agy_badge,
            "pids": [p["pid"] for p in (agy_cli_procs + ffmpeg_procs)],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in (agy_cli_procs + ffmpeg_procs)), 1),
            "current_task": agy_task,
            "speech": agy_speech,
            "logs": agy_logs_out,
        },
        "ceo_claude": {
            "status": claude_status,
            "status_label": claude_badge,
            "current_task": claude_task_desc,
            "speech": claude_speech,
        },
    }


def _check_direct_tasks() -> Dict[str, Dict[str, Any]]:
    """
    Kiểm tra các tác vụ lẻ do Sếp Dũng giao trực tiếp cho từng Agent:
    - Nếu tiến trình còn chạy (`poll() is None`) -> trả về thông tin đang chạy.
    - Ngay khi tiến trình kết thúc -> đóng file log, đẩy vào _DIRECT_HISTORY[agent_id] và xóa khỏi _DIRECT_TASKS.
    """
    active: Dict[str, Dict[str, Any]] = {}
    with _DIRECT_LOCK:
        for ag_id, info in list(_DIRECT_TASKS.items()):
            proc = info.get("proc")
            if proc is not None and proc.poll() is None:
                active[ag_id] = info
            else:
                exit_code = proc.poll() if proc is not None else 0
                fh = info.get("log_fh")
                if fh:
                    try:
                        fh.close()
                    except Exception:
                        pass
                fin_str = _fmt_time(time.time())
                entry = (
                    f"👑 [Lịch sử Lệnh Sếp - {fin_str}] Đã xong (exit={exit_code}): "
                    f"{info.get('title', 'Công việc lẻ')} → Log: {Path(info.get('log_file', '')).name}"
                )
                _DIRECT_HISTORY.setdefault(ag_id, []).insert(0, entry)
                _DIRECT_HISTORY[ag_id] = _DIRECT_HISTORY[ag_id][:10]
                del _DIRECT_TASKS[ag_id]
    return active


def _launch_direct_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Khởi chạy công việc lẻ trực tiếp từ Sếp Dũng cho một Agent (không qua CEO Claude)."""
    agent_id = str(payload.get("agent_id") or "").strip()
    prompt_raw = str(payload.get("prompt") or "").strip()
    title_raw = str(payload.get("title") or "").strip()
    workdir_raw = str(payload.get("workdir") or r"G:\K07").strip()
    effort = str(payload.get("effort") or "medium").strip()
    allow_browser = bool(payload.get("allow_browser", False))
    full_access = bool(payload.get("full_access", False))
    voice_id = str(payload.get("voice_id") or "GIọng Kênh Phật Pháp").strip()

    if not agent_id or not prompt_raw:
        return {"ok": False, "error": "Vui lòng nhập nội dung công việc cần giao."}

    if not title_raw:
        first_ln = prompt_raw.splitlines()[0].strip().lstrip("#").strip()
        title_raw = first_ln[:85] if first_ln else "Công việc lẻ từ Sếp Dũng"

    DIRECT_DIR.mkdir(parents=True, exist_ok=True)
    k07_logs = Path(r"G:\K07\logs")
    k07_logs.mkdir(parents=True, exist_ok=True)

    ts_slug = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    brief_file = DIRECT_DIR / f"{agent_id}_direct_{ts_slug}.md"
    log_file = k07_logs / f"{agent_id}_direct_{ts_slug}.log"

    brief_text = f"# {title_raw}\n\n{prompt_raw}\n"
    brief_file.write_text(brief_text, encoding="utf-8")

    if not os.path.isdir(workdir_raw):
        workdir_raw = r"G:\K07"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd: List[str] = []
    if agent_id in ("codex1", "codex2", "codex3"):
        acct = "1" if agent_id == "codex1" else ("2" if agent_id == "codex2" else "3")
        # Codex bắt buộc dùng đường dẫn thực (resolve junction G:\K07) để không lỗi sandbox
        try:
            real_workdir = str(Path(workdir_raw).resolve())
        except Exception:
            real_workdir = workdir_raw
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            r"C:\Users\Dung\.claude\harness\agents\run_codex.ps1",
            "-PromptFile",
            str(brief_file),
            "-Account",
            acct,
            "-Effort",
            effort if effort in ("low", "medium", "high") else "medium",
            "-SkipGitCheck",
            "-Dir",
            real_workdir,
            "-Log",
            str(log_file),
        ]
        if allow_browser:
            cmd.append("-AllowComputerUse")
        if full_access:
            cmd.append("-FullAccess")

    elif agent_id == "agy":
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            r"C:\Users\Dung\.claude\harness\agents\run_agy.ps1",
            "-PromptFile",
            str(brief_file),
            "-Effort",
            effort if effort in ("low", "medium", "high") else "medium",
            "-Dir",
            workdir_raw,
            "-Log",
            str(log_file),
        ]

    elif agent_id in ("muse1", "muse2"):
        muse_num = "1" if agent_id == "muse1" else "2"
        env["MUSE"] = muse_num
        out_dir = workdir_raw if workdir_raw != r"G:\K07" else r"G:\K07\T02B\Assets\art_raw"
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            r"C:\Users\Dung\.claude\harness\agents\muse\muse.py",
            "--msg",
            str(brief_file),
            "--out",
            out_dir,
            "--new",
            "--grab",
        ]

    elif agent_id == "cit_voice":
        out_wav = Path(workdir_raw) / f"Vo_Direct_{ts_slug}.wav"
        py_code = (
            "import json, urllib.request, sys\n"
            f"p = open(r'{brief_file}', encoding='utf-8').read().splitlines()[2:]\n"
            "txt = '\\n'.join(p).strip()\n"
            f"print('Đang thu âm qua CIT Voice Studio (Port 8001) -> {out_wav} ...', flush=True)\n"
            f"body = json.dumps({{'text': txt, 'voice_id': {voice_id!r}, 'speed': 1.0, 'temperature': 0.45, 'format': 'wav'}}).encode('utf-8')\n"
            "req = urllib.request.Request('http://127.0.0.1:8001/api/tts/generate', data=body, headers={'Content-Type': 'application/json'})\n"
            "raw = urllib.request.urlopen(req, timeout=900).read()\n"
            f"open(r'{out_wav}', 'wb').write(raw)\n"
            f"print('HOÀN TẤT THU ÂM: {out_wav} (' + str(round(len(raw)/1048576, 2)) + ' MB)', flush=True)\n"
        )
        cmd = [sys.executable, "-c", py_code]

    elif agent_id == "ceo_claude":
        claude_exe = r"C:\Users\Dung\.local\bin\claude.exe"
        if not os.path.isfile(claude_exe):
            claude_exe = "claude"
        cmd = [claude_exe, "-p", prompt_raw]
    else:
        return {"ok": False, "error": f"Agent '{agent_id}' không hỗ trợ nhận việc lẻ."}

    try:
        log_fh = open(log_file, "w", encoding="utf-8", errors="replace")
        log_fh.write(f"=== [LỆNH TRỰC TIẾP TỪ SẾP DŨNG] ({datetime.datetime.now().strftime('%H:%M:%S %d/%m/%Y')}) ===\n")
        log_fh.write(f"Agent: {agent_id} | Tiêu đề: {title_raw}\n")
        log_fh.write(f"Thư mục: {workdir_raw}\n")
        log_fh.write("------------------------------------------------------------\n")
        log_fh.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=workdir_raw,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        with _DIRECT_LOCK:
            _DIRECT_TASKS[agent_id] = {
                "proc": proc,
                "pid": proc.pid,
                "agent_id": agent_id,
                "title": title_raw,
                "prompt": prompt_raw[:300],
                "workdir": workdir_raw,
                "brief_file": str(brief_file),
                "log_file": str(log_file),
                "log_fh": log_fh,
                "started_at": time.time(),
                "started_str": datetime.datetime.now().strftime("%H:%M:%S"),
            }
        return {
            "ok": True,
            "pid": proc.pid,
            "agent_id": agent_id,
            "title": title_raw,
            "log_file": str(log_file),
            "message": f"Đã giao việc trực tiếp cho {agent_id.upper()} (PID {proc.pid}): {title_raw}",
        }
    except Exception as e:
        return {"ok": False, "error": f"Không thể khởi chạy lệnh: {e}"}


def _collect_telemetry() -> Dict[str, Any]:
    raw = _query_gpu_and_processes()
    gpu = raw["gpu"]
    all_procs = raw["procs"]

    py_raw = [
        p for p in all_procs
        if str(p.get("name", "")).lower().startswith("python")
        or "cit-voice-studio" in str(p.get("name", "")).lower()
    ]
    py_procs = [_classify_python_process(p, all_procs) for p in py_raw]
    py_procs.sort(key=lambda x: (x["vram_mb"], x["effective_vram_mb"], x["ram_mb"]), reverse=True)

    python_vram_mb = round(sum(float(p.get("vram_mb") or 0.0) for p in py_procs), 1)
    cit_procs = [p for p in py_procs if p.get("category") in ("cit_voice", "omnivoice")]
    cit_vram_mb = round(sum(float(p.get("vram_mb") or 0.0) for p in cit_procs), 1)

    # Kiểm tra API của CIT Voice Studio (cache 30s để không làm nghẽn khi CIT đang thu âm)
    cit_online = False
    if cit_procs:
        now_t = time.time()
        if (now_t - _CIT_HEALTH_CACHE["ts"]) > 30.0:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=0.25) as resp:
                    ch = json.loads(resp.read().decode("utf-8", errors="replace"))
                    _CIT_HEALTH_CACHE["online"] = ch.get("status") == "ok"
                    _CIT_HEALTH_CACHE["engine"] = ch.get("engine") or "VieNeu-TTS-v3-Turbo (GPU • 48kHz)"
            except Exception:
                _CIT_HEALTH_CACHE["online"] = True
            _CIT_HEALTH_CACHE["ts"] = now_t
        cit_online = _CIT_HEALTH_CACHE["online"]
    cit_engine = _CIT_HEALTH_CACHE["engine"]

    k07_root = Path(r"G:\K07")
    k07_t02 = k07_root / "T02"
    state_lines = _read_tail(k07_root / "STATE.md", 10)

    # Quét lịch sử các file giọng đọc Vo*.wav / voice_hoi/H*.wav trên toàn bộ dự án + G:\K07
    voice_history_logs: List[str] = []
    cit_worker_procs = [p for p in py_procs if p.get("category") == "cit_voice_worker"]
    cit_is_generating = len(cit_worker_procs) > 0
    latest_vo_name = ""
    if cit_worker_procs:
        w0 = cit_worker_procs[0]
        w_cmd = w0.get("cmd") or ""
        w_cwd = w0.get("cwd") or ""
        m_script = re.search(r'([A-Za-z]:[\\/][^\s"]+\.py|[^\s"\\/]+\.py)', w_cmd)
        script_path_str = m_script.group(1) if m_script else "make_voice.py"
        script_name = Path(script_path_str).name
        proj_dir_str = str(Path(script_path_str).parent) if ":" in script_path_str else w_cwd
        proj_lbl = _short_project_label(proj_dir_str)
        latest_vo_name = f"[{proj_lbl}] {script_name} (PID {w0['pid']})"
        voice_history_logs.append(f"🟢 ĐANG CHẠY TRỰC TIẾP: {latest_vo_name}")
    try:
        vo_files = sorted(
            list(k07_root.glob("T*/Final/Vo*.wav"))
            + list(k07_root.glob("T*/Final/Vo*.mp3"))
            + list(k07_root.glob("T*/voice_hoi/*.wav")),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for vf in vo_files[:6]:
            v_mt = vf.stat().st_mtime
            v_mb = vf.stat().st_size / (1024 * 1024)
            rel_v = vf.relative_to(k07_root)
            if not latest_vo_name:
                latest_vo_name = str(rel_v)
            if (time.time() - v_mt) < 90:
                cit_is_generating = True
            voice_history_logs.append(
                f"🕒 [Lịch sử - {_fmt_time(v_mt)}] Đã thu xong: {rel_v} ({v_mb:.1f} MB)"
            )
    except Exception:
        pass

    cit_log_file = Path(r"E:\CIT Voice Studio\cit-voice-studio.log")
    if not cit_log_file.is_file():
        cit_log_file = Path(r"D:\AI Local\CIT Voice Studio\cit-voice-studio.log")
    cit_log_tail = _read_tail(cit_log_file, 6)

    muse1_info = _parse_muse_status(1, k07_t02, py_procs)
    muse2_info = _parse_muse_status(2, k07_t02, py_procs)

    sync_info = _inspect_claude_sync_and_k07_agents(k07_root, all_procs)
    claude_sync = sync_info["claude_sync"]
    c1_info = sync_info["codex1"]
    c2_info = sync_info["codex2"]
    c3_info = sync_info["codex3"]
    agy_info = sync_info["agy"]
    ceo_info = sync_info["ceo_claude"]

    # Bổ sung các tiến trình Muse / CIT Voice đang chạy thực tế vào bảng đồng bộ Claude
    extra_live_tasks: List[Dict[str, Any]] = []
    if muse1_info["is_running"]:
        extra_live_tasks.append({
            "task_id": f"pid_{muse1_info['pids'][0]}",
            "label": f"Muse 1: {muse1_info['speech']}",
            "is_running": True,
            "exit_code": None,
            "mtime": time.time(),
            "time_str": _fmt_time(time.time()),
            "size_kb": 0.0,
            "last_line": muse1_info["task_summary"],
        })
    if muse2_info["is_running"]:
        extra_live_tasks.append({
            "task_id": f"pid_{muse2_info['pids'][0]}",
            "label": f"Muse 2: {muse2_info['speech']}",
            "is_running": True,
            "exit_code": None,
            "mtime": time.time(),
            "time_str": _fmt_time(time.time()),
            "size_kb": 0.0,
            "last_line": muse2_info["task_summary"],
        })
    if cit_worker_procs:
        extra_live_tasks.append({
            "task_id": f"pid_{cit_worker_procs[0]['pid']}",
            "label": f"CIT Voice: {latest_vo_name}",
            "is_running": True,
            "exit_code": None,
            "mtime": time.time(),
            "time_str": _fmt_time(time.time()),
            "size_kb": 0.0,
            "last_line": f"Đang chạy script thu âm CIT Voice Studio ({latest_vo_name})",
        })
    if extra_live_tasks:
        claude_sync["recent_tasks"] = extra_live_tasks + claude_sync["recent_tasks"]
        claude_sync["active_tasks_count"] = sum(1 for t in claude_sync["recent_tasks"] if t.get("is_running"))

    claude_procs = [p for p in all_procs if "claude" in str(p.get("name", "")).lower()]

    # Tổng hợp xem hiện tại những Agent nào đang thực sự làm việc
    active_work_names: List[str] = []
    if ceo_info["status"] == "working":
        active_work_names.append("CEO Claude")
    if c1_info["status"] == "working":
        active_work_names.append("Codex 1")
    if agy_info["status"] == "working":
        active_work_names.append("Agy")
    if c2_info["status"] == "working":
        active_work_names.append("Codex 2")
    if c3_info["status"] == "working":
        active_work_names.append("Codex 3")
    if muse1_info["is_running"]:
        active_work_names.append("Muse 1")
    if muse2_info["is_running"]:
        active_work_names.append("Muse 2")
    if cit_is_generating:
        active_work_names.append("CIT Voice")

    if active_work_names:
        office_live_summary = f"Đang làm việc thực tế: {', '.join(active_work_names)}"
    else:
        office_live_summary = "Tất cả các Agent con hiện đang nghỉ chờ lệnh mới"

    ceo_logs: List[str] = [
        f"🔔 [Tình trạng hiện tại] Đang chạy {claude_sync['active_tasks_count']} tiến trình ngầm (tasks/*.output):"
    ]
    for t in claude_sync["recent_tasks"][:6]:
        state_icon = "🟢 ĐANG CHẠY" if t["is_running"] else f"🕒 [Lịch sử - Đã xong (exit={t['exit_code']})]"
        ceo_logs.append(f"  • [{t['time_str']}] #{t['task_id']} ({t['label']}): {state_icon} — {t['last_line']}")
    if state_lines:
        ceo_logs.append("--- Nội dung STATE.md ---")
        ceo_logs.extend(state_lines[:6])

    agents: List[Dict[str, Any]] = [
        {
            "id": "chairman",
            "name": "Sếp Dũng",
            "title": "SẾP TỔNG — CHỦ TỌA HỆ THỐNG",
            "desk_zone": "Phòng Sếp Dũng (VIP Penthouse)",
            "model_account": "Chủ sở hữu hệ thống RTX 5070 Ti",
            "status": "working",
            "status_label": "ĐANG ĐIỀU HÀNH",
            "color": "#f59e0b",
            "pids": [],
            "ram_mb": 0,
            "vram_mb": 0,
            "current_task": f"Giám sát Trụ Sở 3D • {office_live_summary}",
            "speech": f"👑 {office_live_summary}",
            "responsibilities": "Giao việc, duyệt Plan (G0), duyệt chặng ★ và nghiệm thu sản phẩm cuối.",
            "logs": [
                f"Trạng thái văn phòng: {office_live_summary}",
                f"GPU RTX 5070 Ti: {gpu['vram_used_mb']:,} / {gpu['vram_total_mb']:,} MB VRAM (Python chiếm {python_vram_mb} MB)",
            ] + state_lines[:5],
            "position": [0, 0, -10.5],
        },
        {
            "id": "ceo_claude",
            "name": "CEO Claude",
            "title": "TỔNG GIÁM ĐỐC ĐIỀU PHỐI (CEO)",
            "desk_zone": "Phòng Điều Hành & Quản Lý",
            "model_account": "Sonnet 5.5 + Advisor Opus 5.5",
            "status": ceo_info["status"] if claude_procs else "idle",
            "status_label": ceo_info["status_label"] if claude_procs else "ĐÃ TẮT",
            "color": "#a855f7",
            "pids": [p["pid"] for p in claude_procs],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in claude_procs), 1),
            "vram_mb": round(sum(float(p.get("vram_mb") or 0) for p in claude_procs), 1),
            "current_task": ceo_info["current_task"] if claude_procs else "Hiện đang tắt — Chưa mở ứng dụng Claude.",
            "speech": ceo_info["speech"] if claude_procs else "💤 Đang nghỉ",
            "responsibilities": "Lập plan G0, viết brief G1, giao việc qua ống dẫn Auto-Wakeup (tasks/*.output), nghiệm thu G2-G6.",
            "logs": ceo_logs,
            "position": [-2.8, 0, -4.2],
        },
        {
            "id": "codex1",
            "name": "Codex 1 (Quản Lý)",
            "title": "QUẢN LÝ QA & ĐIỀU KHIỂN TRÌNH DUYỆT",
            "desk_zone": "Phòng Điều Hành & Quản Lý",
            "model_account": "dungcao2112 (Plus) • -Account 1",
            "status": c1_info["status"],
            "status_label": c1_info["status_label"],
            "color": "#3b82f6",
            "pids": c1_info["pids"],
            "ram_mb": c1_info["ram_mb"],
            "vram_mb": 0.0,
            "current_task": c1_info["current_task"],
            "speech": c1_info["speech"],
            "responsibilities": "Soát chất lượng ảnh/kịch bản, viết reports/*.md, điều khiển trình duyệt (-AllowComputerUse).",
            "logs": c1_info["logs"],
            "position": [2.8, 0, -4.2],
        },
        {
            "id": "agy",
            "name": "Agy (Antigravity)",
            "title": "NHÂN VIÊN CẤP CAO (KỸ THUẬT & 3D)",
            "desk_zone": "Phòng Kỹ Thuật & Lập Trình",
            "model_account": "Gemini 4 Argon (High) • run_agy.ps1",
            "status": agy_info["status"],
            "status_label": agy_info["status_label"],
            "color": "#06b6d4",
            "pids": agy_info["pids"],
            "ram_mb": agy_info["ram_mb"],
            "vram_mb": 0.0,
            "current_task": agy_info["current_task"],
            "speech": agy_info["speech"],
            "responsibilities": "Ưu tiên số 1 cho việc kỹ thuật: Python, FFmpeg/FFprobe, xử lý dữ liệu, căn chỉnh phụ đề, ghép video, nghiên cứu.",
            "logs": agy_info["logs"],
            "position": [-7.5, 0, -1.5],
        },
        {
            "id": "codex2",
            "name": "Codex 2 (Kỹ Thuật Chính)",
            "title": "CHUYÊN GIA CODE, BIBLE & REMOTION",
            "desk_zone": "Phòng Kỹ Thuật & Lập Trình",
            "model_account": "hoaidaik1+2026 (Plus) • -Account 2",
            "status": c2_info["status"],
            "status_label": c2_info["status_label"],
            "color": "#6366f1",
            "pids": c2_info["pids"],
            "ram_mb": c2_info["ram_mb"],
            "vram_mb": 0.0,
            "current_task": c2_info["current_task"],
            "speech": c2_info["speech"],
            "responsibilities": "Viết kịch bản, thiết kế Bible & Prompts, code phức tạp, dựng Remotion (-FullAccess), dịch ngược REA.",
            "logs": c2_info["logs"],
            "position": [-7.5, 0, 2.2],
        },
        {
            "id": "codex3",
            "name": "Codex 3 (Chữa Cháy / Đa Nhiệm)",
            "title": "NHÂN SỰ TĂNG CƯỜNG & CHẠY SONG SONG",
            "desk_zone": "Phòng Kỹ Thuật & Lập Trình",
            "model_account": "dungch.eraland (Free) • -Account 3 / Đa nhiệm",
            "status": c3_info["status"],
            "status_label": c3_info["status_label"],
            "color": "#94a3b8",
            "pids": c3_info["pids"],
            "ram_mb": c3_info["ram_mb"],
            "vram_mb": 0.0,
            "current_task": c3_info["current_task"],
            "speech": c3_info["speech"],
            "responsibilities": "Chữa cháy việc nhỏ (-Effort low) và tự động nhận luồng chạy song song khi Claude giao nhiều task Codex cùng lúc.",
            "logs": c3_info["logs"],
            "position": [-7.5, 0, 5.8],
        },
        {
            "id": "muse1",
            "name": "Muse 1 (Họa Sĩ AI)",
            "title": "STUDIO TRANH GHIBLI & I2V #1",
            "desk_zone": "Phòng Thiết Kế Ghibli",
            "model_account": "GPM Profile MUSE 1 • muse_batch.py",
            "status": "working" if muse1_info["is_running"] else "idle",
            "status_label": "ĐANG VẼ TRANH" if muse1_info["is_running"] else "ĐANG NGHỈ",
            "color": "#10b981",
            "pids": muse1_info["pids"],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in py_procs if p.get("agent_id") == "muse1"), 1),
            "vram_mb": 0.0,
            "current_task": muse1_info["task_summary"],
            "speech": muse1_info["speech"],
            "responsibilities": "Sinh ảnh nền 16:9 (2048×1152) phong cách Studio Ghibli thông qua GPM Profile MUSE 1.",
            "logs": muse1_info["log_tail"],
            "latest_art_url": muse1_info["latest_art_url"],
            "latest_art_name": muse1_info["latest_art_name"],
            "progress_pct": muse1_info["progress_pct"],
            "done_count": muse1_info["done_count"],
            "total_count": muse1_info["total_count"],
            "position": [7.5, 0, -2.5],
        },
        {
            "id": "muse2",
            "name": "Muse 2 (Họa Sĩ AI)",
            "title": "STUDIO TRANH GHIBLI & I2V #2",
            "desk_zone": "Phòng Thiết Kế Ghibli",
            "model_account": "GPM Profile MUSE 2 • muse_batch.py",
            "status": "working" if muse2_info["is_running"] else "idle",
            "status_label": "ĐANG VẼ TRANH" if muse2_info["is_running"] else "ĐANG NGHỈ",
            "color": "#34d399",
            "pids": muse2_info["pids"],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in py_procs if p.get("agent_id") == "muse2"), 1),
            "vram_mb": 0.0,
            "current_task": muse2_info["task_summary"],
            "speech": muse2_info["speech"],
            "responsibilities": "Sinh ảnh nền 16:9 (2048×1152) phong cách Studio Ghibli thông qua GPM Profile MUSE 2.",
            "logs": muse2_info["log_tail"],
            "latest_art_url": muse2_info["latest_art_url"],
            "latest_art_name": muse2_info["latest_art_name"],
            "progress_pct": muse2_info["progress_pct"],
            "done_count": muse2_info["done_count"],
            "total_count": muse2_info["total_count"],
            "position": [7.5, 0, 2.2],
        },
        {
            "id": "cit_voice",
            "name": "CIT Voice Studio",
            "title": "PHÒNG THU GIỌNG NÓI AI (VIENEU-TTS V3 TURBO)",
            "desk_zone": "Phòng Server & Âm Thanh",
            "model_account": f"{cit_engine} • Port 8001",
            "status": (
                "working"
                if cit_is_generating
                else ("standby" if (cit_procs or cit_online) else "idle")
            ),
            "status_label": (
                "ĐANG THU GIỌNG AI"
                if cit_is_generating
                else (
                    f"ĐANG BẬT SẴN ({cit_vram_mb/1024:.1f} GB VRAM)"
                    if (cit_procs or cit_online)
                    else "ĐANG NGHỈ (0 MB VRAM)"
                )
            ),
            "color": (
                "#10b981"
                if cit_is_generating
                else ("#f59e0b" if (cit_procs or cit_online) else "#64748b")
            ),
            "pids": [p["pid"] for p in cit_procs],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in cit_procs), 1),
            "vram_mb": cit_vram_mb,
            "current_task": (
                f"🎙️ ĐANG TỔNG HỢP GIỌNG ĐỌC: {latest_vo_name} ('Giọng Kênh Phật Pháp' • temp 0.45 • 48kHz)"
                if cit_is_generating
                else (
                    f"Hiện đang nghỉ (Ứng dụng đang mở sẵn trên Port 8001, giữ {cit_vram_mb:,.0f} MB VRAM — Sẵn sàng nhận kịch bản mới)."
                    if (cit_procs or cit_online)
                    else "Hiện đang nghỉ — Đã đóng ứng dụng (0 MB VRAM)."
                )
            ),
            "speech": (
                f"🎙️ Đang thu: {latest_vo_name}"
                if cit_is_generating
                else (
                    f"🎙️ Bật sẵn Port 8001 ({cit_vram_mb:,.0f} MB VRAM)"
                    if (cit_procs or cit_online)
                    else "💤 Đang tắt (0 MB VRAM)"
                )
            ),
            "responsibilities": "Tổng hợp giọng đọc nguyên khối 'Giọng Kênh Phật Pháp' (temp 0.45, 48kHz) + xuất phụ đề .srt qua CIT Voice Studio (http://127.0.0.1:8001).",
            "logs": (
                [
                    f"🔔 Tình trạng hiện tại: {'ĐANG THU GIỌNG' if cit_is_generating else ('Đang mở sẵn chờ lệnh (Port 8001 • ' + cit_engine + ')' if (cit_procs or cit_online) else 'Đang tắt (0 MB VRAM)')}",
                    "--- Lịch sử các bản thu giọng K07 đã hoàn thành ---",
                ]
                + voice_history_logs
                + ["--- Nhật ký khởi động CIT Voice Studio ---"]
                + cit_log_tail
            ),
            "position": [3.2, 0, 6.5],
        },
        {
            "id": "aux_services",
            "name": "Trạm Dịch Vụ Nền",
            "title": "VĂN PHÒNG 3D, BLENDER MCP & LAN SERVER",
            "desk_zone": "Phòng Server & Âm Thanh",
            "model_account": "VanPhong3D + mcp-for-blender + lan-transfer",
            "status": "standby",
            "status_label": "CHẠY NỀN (0 MB VRAM)",
            "color": "#14b8a6",
            "pids": [p["pid"] for p in py_procs if p.get("category") in ("mcp", "utility", "self")],
            "ram_mb": round(sum(float(p.get("ram_mb") or 0) for p in py_procs if p.get("category") in ("mcp", "utility", "self")), 1),
            "vram_mb": 0.0,
            "current_task": "Đang duy trì Server Văn Phòng 3D (Port 7890), Blender MCP & LAN Transfer (0 MB VRAM).",
            "speech": "🔌 Chạy nền ổn định (0 MB VRAM)",
            "responsibilities": "Duy trì Văn Phòng 3D độc lập, xuất cảnh Blender và truyền dữ liệu nội bộ.",
            "logs": [
                f"{p['role']} (PID {p['pid']}): {p['ram_mb']} MB RAM, 0 MB VRAM"
                for p in py_procs
                if p.get("category") in ("mcp", "utility", "self")
            ],
            "position": [-2.8, 0, 6.5],
        },
    ]

    direct_active = _check_direct_tasks()
    for ag in agents:
        aid = ag["id"]
        d_hist = _DIRECT_HISTORY.get(aid, [])
        if aid in direct_active:
            dt = direct_active[aid]
            ag["status"] = "working"
            ag["status_label"] = "ĐANG LÀM (LỆNH SẾP)"
            ag["direct_task_running"] = True
            if dt["pid"] not in ag["pids"]:
                ag["pids"] = [dt["pid"]] + ag["pids"]
            ag["current_task"] = (
                f"👑 LỆNH TRỰC TIẾP TỪ SẾP DŨNG (PID {dt['pid']} • Bắt đầu {dt['started_str']}): {dt['title']}"
            )
            ag["speech"] = f"👑 Lệnh Sếp: {dt['title'][:48]}..."
            live_tail = _read_tail(Path(dt["log_file"]), 10)
            ag["logs"] = (
                [
                    f"🟢 ĐANG CHẠY LỆNH SẾP DŨNG (PID {dt['pid']}): {dt['title']}",
                    f"📂 Thư mục: {dt['workdir']} | Log: {Path(dt['log_file']).name}",
                    "--- Đầu ra trực tiếp ---",
                ]
                + live_tail
                + ["--- Lịch sử trước đó ---"]
                + d_hist
                + ag.get("logs", [])
            )
            if ag["name"] not in active_work_names:
                active_work_names.append(ag["name"])
        else:
            ag["direct_task_running"] = False
            if d_hist:
                ag["logs"] = ag.get("logs", [])[:1] + d_hist + ag.get("logs", [])[1:]

    if active_work_names:
        office_live_summary = f"Đang làm việc thực tế: {', '.join(active_work_names)}"
        agents[0]["current_task"] = f"Giám sát Trụ Sở 3D • {office_live_summary}"
        agents[0]["speech"] = f"👑 {office_live_summary}"

    return {
        "ok": True,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
        "gpu": gpu,
        "vram_summary": {
            "python_vram_mb": python_vram_mb,
            "omnivoice_vram_mb": cit_vram_mb,
            "omnivoice_running": len(cit_procs) > 0,
            "omnivoice_pids": [p["pid"] for p in cit_procs],
            "other_vram_mb": max(0.0, round(gpu["vram_used_mb"] - python_vram_mb, 1)),
            "diagnosis": (
                f"CIT Voice Studio (PID {', '.join(str(p['pid']) for p in cit_procs)}) đang mở sẵn trên Port 8001 và dùng {cit_vram_mb:,.0f} MB VRAM."
                if cit_vram_mb > 100
                else "Không có tiến trình AI Voice/Python nào chiếm dụng VRAM (0 MB VRAM)."
            ),
        },
        "python_processes": py_procs,
        "agents": agents,
        "claude_sync": claude_sync,
        "project_k07": {
            "name": "G:\\K07",
            "office_live_summary": office_live_summary,
            "active_agents": active_work_names,
            "muse1": muse1_info,
            "muse2": muse2_info,
        },
    }


def _telemetry_loop() -> None:
    global _TELEMETRY_CACHE
    while True:
        try:
            data = _collect_telemetry()
            with _TELEMETRY_LOCK:
                _TELEMETRY_CACHE = data
        except Exception:
            pass
        time.sleep(3.5)


def _kill_pid_tree(pid: int) -> Dict[str, Any]:
    if pid == os.getpid():
        return {"ok": False, "error": "Không thể tự tắt Server Văn Phòng 3D."}
    out = _run_hidden(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=6)
    return {"ok": True, "pid": pid, "output": out}


class OfficeRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        global _TELEMETRY_CACHE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/telemetry":
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("fresh") == ["1"]:
                data = _collect_telemetry()
                with _TELEMETRY_LOCK:
                    _TELEMETRY_CACHE = data
            else:
                with _TELEMETRY_LOCK:
                    data = _TELEMETRY_CACHE
            self._send_json(data)
            return

        if path == "/api/latest-art":
            qs = urllib.parse.parse_qs(parsed.query)
            muse_num = int((qs.get("muse") or ["1"])[0])
            art_dir = Path(r"G:\K07\T02\Assets\art_raw")
            s_start = 1 if muse_num == 1 else 67
            s_end = 66 if muse_num == 1 else 132
            latest_file: Optional[Path] = None
            latest_mt = 0.0
            if art_dir.is_dir():
                for i in range(s_start, s_end + 1):
                    p = art_dir / f"A{i:03d}.png"
                    if p.is_file():
                        mt = p.stat().st_mtime
                        if mt > latest_mt:
                            latest_mt = mt
                            latest_file = p
            if latest_file and latest_file.is_file():
                raw = latest_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_error(404, "No art found")
            return

        if path == "/" or path == "":
            file_path = WEB_DIR / "index.html"
        elif path == "/favicon.ico" and not (WEB_DIR / "favicon.ico").is_file():
            file_path = WEB_DIR / "favicon.svg"
        else:
            rel = path.lstrip("/").replace("..", "")
            file_path = WEB_DIR / rel

        if file_path.is_file():
            ctype, _ = mimetypes.guess_type(str(file_path))
            if not ctype:
                ctype = "text/html; charset=utf-8" if file_path.suffix == ".html" else "application/octet-stream"
            elif ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            raw = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        global _TELEMETRY_CACHE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
        try:
            payload = json.loads(body_raw) if body_raw else {}
        except Exception:
            payload = {}

        if path == "/api/free-omnivoice-vram":
            with _TELEMETRY_LOCK:
                current = _TELEMETRY_CACHE
            before_vram = current.get("gpu", {}).get("vram_used_mb", 0)
            killed_pids: List[int] = []
            for p in current.get("python_processes", []):
                if p.get("category") in ("cit_voice", "omnivoice"):
                    _kill_pid_tree(int(p["pid"]))
                    killed_pids.append(int(p["pid"]))
            time.sleep(1.2)
            fresh = _collect_telemetry()
            with _TELEMETRY_LOCK:
                _TELEMETRY_CACHE = fresh
            after_vram = fresh.get("gpu", {}).get("vram_used_mb", 0)
            freed = max(0, before_vram - after_vram)
            self._send_json(
                {
                    "ok": True,
                    "killed_pids": killed_pids,
                    "before_vram_mb": before_vram,
                    "after_vram_mb": after_vram,
                    "freed_vram_mb": freed,
                    "message": f"Đã đóng CIT Voice Studio (PID {killed_pids}) và giải phóng ~{freed:,} MB VRAM!",
                    "telemetry": fresh,
                }
            )
            return

        if path == "/api/kill-pid":
            target_pid = int(payload.get("pid") or 0)
            if target_pid <= 0:
                self._send_json({"ok": False, "error": "PID không hợp lệ"}, 400)
                return
            res = _kill_pid_tree(target_pid)
            time.sleep(0.8)
            fresh = _collect_telemetry()
            with _TELEMETRY_LOCK:
                _TELEMETRY_CACHE = fresh
            res["telemetry"] = fresh
            self._send_json(res)
            return

        if path == "/api/start-omnivoice":
            cit_exe = Path(r"E:\CIT Voice Studio\CIT-Voice-Studio.exe")
            if not cit_exe.is_file():
                cit_exe = Path(r"D:\AI Local\CIT Voice Studio\CIT-Voice-Studio.exe")
            if cit_exe.is_file():
                subprocess.Popen(
                    [str(cit_exe)],
                    cwd=str(cit_exe.parent),
                    close_fds=True,
                )
                self._send_json({"ok": True, "message": "Đang khởi động CIT Voice Studio (Port 8001)..."})
            else:
                self._send_json({"ok": False, "error": "Không tìm thấy CIT-Voice-Studio.exe"}, 404)
            return

        if path == "/api/open-folder":
            target = str(payload.get("path") or r"G:\K07\T02")
            if os.path.exists(target):
                os.startfile(target)
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": f"Không tìm thấy đường dẫn: {target}"}, 404)
            return

        if path == "/api/assign-task":
            res = _launch_direct_task(payload)
            if res.get("ok"):
                time.sleep(0.4)
                fresh = _collect_telemetry()
                with _TELEMETRY_LOCK:
                    _TELEMETRY_CACHE = fresh
                res["telemetry"] = fresh
                self._send_json(res, 200)
            else:
                self._send_json(res, 400)
            return

        if path == "/api/stop-direct-task":
            agent_id = str(payload.get("agent_id") or "").strip()
            stopped_pid = 0
            with _DIRECT_LOCK:
                dt = _DIRECT_TASKS.get(agent_id)
                if dt and dt.get("pid"):
                    stopped_pid = int(dt["pid"])
            if stopped_pid > 0:
                _kill_pid_tree(stopped_pid)
                time.sleep(0.5)
                fresh = _collect_telemetry()
                with _TELEMETRY_LOCK:
                    _TELEMETRY_CACHE = fresh
                self._send_json(
                    {
                        "ok": True,
                        "message": f"Đã dừng công việc lẻ của {agent_id.upper()} (PID {stopped_pid}).",
                        "telemetry": fresh,
                    }
                )
            else:
                self._send_json({"ok": False, "error": "Không có tác vụ lẻ nào đang chạy trên Agent này."}, 404)
            return

        self._send_json({"ok": False, "error": "Unknown endpoint"}, 404)


def main() -> None:
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    t = threading.Thread(target=_telemetry_loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), OfficeRequestHandler)
    try:
        if sys.stdout is not None:
            print(f"Van Phong 3D AI Agents Server dang chay tai http://{HOST}:{PORT}", flush=True)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
