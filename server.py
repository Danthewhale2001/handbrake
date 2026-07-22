#!/usr/bin/env python3
"""
HandBrake Mobile UI v2 - Backend Server
Full feature parity with HandBrake desktop app.
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import subprocess, threading, json, os, re, uuid, time, hashlib, shutil
from pathlib import Path

OUTPUT_PATH  = os.environ.get("OUTPUT_PATH", "/output")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8888"))
CONFIG_FILE  = "/config/hb-mobile-config.json"

app = Flask(__name__, static_folder="static")
CORS(app)

jobs      = {}
jobs_lock = threading.Lock()
QUEUE_FILE   = "/config/hb-queue.json"
SESSION_FILE = "/config/hb-session.json"

def _clear_session_source():
    """Clear source/output state on container restart so app starts fresh."""
    try:
        with open(SESSION_FILE) as f:
            session = json.load(f)
        session.pop("sourceFile", None)
        session.pop("scannedTitleFiles", None)
        session.pop("outputFolder", None)
        session.pop("toPathLabel", None)
        session.pop("saveAs", None)
        with open(SESSION_FILE, "w") as f:
            json.dump(session, f, indent=2)
    except Exception:
        pass

_clear_session_source()
PREFS_FILE   = "/config/hb-prefs.json"

PREFS_DEFAULTS = {
    # General
    "auto_naming": True,
    "auto_name_template": "{source}",
    "mp4_extension": False,
    "num_previews": 10,
    "min_title_duration": 10,
    "max_title_duration_enabled": False,
    "max_title_duration": 0,
    "keep_duplicate_titles": False,
    "same_settings_batch": True,
    "show_preview_summary": True,
    "excluded_extensions": ["jpg","png","srt","ssa","ass"],
    # Queue
    "pause_on_low_disk": True,
    "low_disk_threshold_gb": 10,
    "clear_completed_on_encode": False,
    "pause_on_power_saver": True,
    "when_done_default": "Do Nothing",
    "notify_queue_complete": True,
    "notify_each_complete": False,
    "send_file_to": "",
    # Advanced
    "cq_granularity": 1,
    "use_dvdnav": True,
    "logs_with_movie": False,
    "log_verbosity": 1,
    "log_longevity": "Month",
    "scale_hd_previews": True,
    "auto_scan_dvd": False,
    "activity_font_size": 8,
}

def load_prefs():
    try:
        with open(PREFS_FILE) as f:
            saved = json.load(f)
        return {**PREFS_DEFAULTS, **saved}
    except FileNotFoundError:
        return dict(PREFS_DEFAULTS)
    except Exception:
        return dict(PREFS_DEFAULTS)

def save_prefs(prefs):
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)

def save_queue():
    try:
        with jobs_lock:
            # Only save jobs that aren't actively running (encoding resets on restart anyway)
            saveable = {}
            for jid, job in jobs.items():
                if job.get("status") in ("queued", "done", "error", "cancelled"):
                    saveable[jid] = {k:v for k,v in job.items() if k != "log"}
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w") as f:
            json.dump(saveable, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save queue: {e}")

def load_queue():
    try:
        with open(QUEUE_FILE) as f:
            saved = json.load(f)
        with jobs_lock:
            for jid, job in saved.items():
                # If job was mid-encode when we crashed, reset it to queued
                # Leave any partial output file as-is (it may be playable)
                if job.get("status") in ("encoding", "paused"):
                    job["status"] = "queued"
                    job["progress"] = 0
                    job["eta"] = ""
                    job.pop("started_at", None)
                    job.pop("ended_at", None)
                    job.pop("pid", None)
                    job.pop("output_file", None)
                job["log"] = []
                jobs[jid] = job
        print(f"  Restored {len(saved)} jobs from queue file")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Warning: could not load queue: {e}")

# ── Config (storage locations + presets) ──────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"locations": [], "presets": []}

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Track / file info ─────────────────────────────────────────────────────────

def _hb_scan_audio(filepath):
    """Return dict keyed by 1-based track index with bitrate/samplerate from HB scan."""
    try:
        r = subprocess.run(
            ["HandBrakeCLI", "-i", filepath, "--scan", "--no-dvdnav", "--min-duration", "0"],
            capture_output=True, text=True, timeout=30)
        out = r.stderr
    except Exception:
        return {}
    import re
    result = {}
    track_num = 0
    in_audio = False
    for line in out.splitlines():
        if re.search(r'\+ audio tracks:', line):
            in_audio = True
            track_num = 0
            continue
        if in_audio:
            m = re.match(r'\s+\+\s+(\d+),', line)
            if m:
                track_num = int(m.group(1))
                br_m = re.search(r'\((\d+)\s+kbps\)', line)
                sr_m = re.search(r'\((\d+)\s+Hz\)', line)
                result[track_num] = {
                    "bitrate":    int(br_m.group(1)) if br_m else 0,
                    "samplerate": int(sr_m.group(1)) if sr_m else 0,
                }
            elif line.strip() and not line.strip().startswith('+') and track_num:
                in_audio = False
    return result

def get_track_info(filepath):
    result = subprocess.run(
        ["ffprobe","-v","quiet","-print_format","json",
         "-show_streams","-show_chapters","-show_format", filepath],
        capture_output=True, text=True)
    if result.returncode != 0:
        return {"audio":[],"subtitles":[],"chapters":[],"video":{}}
    try:
        data = json.loads(result.stdout)
    except Exception:
        return {"audio":[],"subtitles":[],"chapters":[],"video":{}}

    audio, subtitles, video_info = [], [], {}
    for s in data.get("streams",[]):
        ct   = s.get("codec_type","")
        tags = s.get("tags",{})
        lang  = tags.get("language","und")
        # Try multiple tag fields for subtitle title
        title = tags.get("title","") or tags.get("handler_name","") or tags.get("name","")
        # Clean up handler names that are just codec descriptions
        if title.lower() in ("subtitle","subtitles","text","subrip","srt","ass","ssa",""):
            title = ""
        disp  = s.get("disposition",{})
        if ct == "video" and not video_info:
            # Parse fps from r_frame_rate fraction e.g. "24000/1001" -> "23.976"
            raw_fps = s.get("r_frame_rate","") or s.get("avg_frame_rate","")
            fps_clean = ""
            if raw_fps and "/" in raw_fps:
                try:
                    num, den = raw_fps.split("/")
                    fps_val = float(num) / float(den)
                    # Round to common values
                    common = {23.976:23.976, 24.0:24, 25.0:25, 29.97:29.97,
                              30.0:30, 47.952:47.952, 48.0:48, 50.0:50,
                              59.94:59.94, 60.0:60}
                    fps_clean = str(min(common, key=lambda x: abs(x-fps_val)))
                    if fps_clean.endswith(".0"): fps_clean = fps_clean[:-2]
                except: fps_clean = raw_fps
            elif raw_fps:
                fps_clean = raw_fps
            # Get duration from stream tags or format (added later)
            duration_secs = 0
            try: duration_secs = float(s.get("duration",0) or 0)
            except: pass
            video_info = {
                "codec":     s.get("codec_name",""),
                "width":     s.get("width",0),
                "height":    s.get("height",0),
                "fps":       fps_clean,
                "duration":  duration_secs,
                "pix_fmt":   s.get("pix_fmt",""),
                "bit_depth": s.get("bits_per_raw_sample",""),
            }
        elif ct == "audio":
            # Get bitrate - try stream bit_rate first, fall back to format bit_rate
            _br = s.get("bit_rate","0") or data.get("format",{}).get("bit_rate","0")
            try: _br_kbps = int(round(int(_br)/1000))
            except: _br_kbps = 0
            # Get sample rate
            try: _sr = int(s.get("sample_rate","48000"))
            except: _sr = 48000
            # Channel layout - prefer descriptive layout over raw count
            _ch_layout = s.get("channel_layout","")
            _ch_count = s.get("channels",2)
            # Format channel layout nicely: 5.1(side) -> 5.1, stereo -> 2.0 etc
            _ch_display = _ch_layout.split("(")[0] if _ch_layout else str(_ch_count)
            if _ch_display == "stereo": _ch_display = "2.0"
            elif _ch_display == "mono": _ch_display = "1.0"
            audio.append({
                "index":          s.get("index",0),
                "track":          len(audio)+1,
                "language":       lang,
                "title":          title,
                "codec":          s.get("codec_name",""),
                "profile":        s.get("profile",""),
                "channels":       _ch_count,
                "channel_layout": _ch_display,
                "samplerate":     _sr,
                "bitrate":        _br_kbps,
                "default":        disp.get("default",0)==1,
                "forced":         disp.get("forced",0)==1,
            })
        elif ct == "subtitle":
            # Build a meaningful title from available metadata
            _sub_lang_map = {
                "eng":"English","jpn":"Japanese","fre":"French","ger":"German",
                "spa":"Spanish","ita":"Italian","por":"Portuguese","rus":"Russian",
                "chi":"Chinese","kor":"Korean","ara":"Arabic","dut":"Dutch",
                "swe":"Swedish","nor":"Norwegian","dan":"Danish","fin":"Finnish",
                "pol":"Polish","hun":"Hungarian","cze":"Czech","rum":"Romanian",
                "tur":"Turkish","heb":"Hebrew","tha":"Thai","vie":"Vietnamese",
                "ind":"Indonesian","may":"Malay","ukr":"Ukrainian","hrv":"Croatian",
                "gre":"Greek","nob":"Norwegian","und":"Unknown",
                "en":"English","ja":"Japanese","fr":"French","de":"German",
                "es":"Spanish","it":"Italian","pt":"Portuguese","ru":"Russian",
                "zh":"Chinese","ko":"Korean","ar":"Arabic","nl":"Dutch",
            }
            lang_name = _sub_lang_map.get(lang, lang.upper() if lang != "und" else "")
            codec_name = s.get("codec_name","").upper().replace("SUBRIP","SRT")
            if title:
                sub_title = title
            elif lang_name:
                sub_title = lang_name
            else:
                sub_title = f"Subtitle {len(subtitles)+1}"
            subtitles.append({
                "index":            s.get("index",0),
                "track":            len(subtitles)+1,
                "language":         lang,
                "title":            sub_title,
                "codec":            s.get("codec_name",""),
                "default":          disp.get("default",0)==1,
                "forced":           disp.get("forced",0)==1,
                "hearing_impaired": disp.get("hearing_impaired",0)==1,
            })

    chapters = []
    for i, ch in enumerate(data.get("chapters",[])):
        tags = ch.get("tags",{})
        start_s = float(ch.get("start_time",0))
        end_s   = float(ch.get("end_time",0))
        dur_s   = end_s - start_s
        def fmt(s):
            h=int(s//3600); m=int((s%3600)//60); sec=s%60
            return f"{h:02d}:{m:02d}:{sec:05.2f}"
        chapters.append({
            "index":    i+1,
            "start":    fmt(start_s),
            "duration": fmt(dur_s),
            "title":    tags.get("title", f"Chapter {i+1}"),
        })

    # If video duration is 0, get it from the format container
    if video_info and not video_info.get("duration"):
        try:
            fmt_duration = float(data.get("format",{}).get("duration",0) or 0)
            video_info["duration"] = fmt_duration
        except: pass

    # Overlay accurate bitrate/samplerate from HB scan (ffprobe misreports these for EAC3/DTS)
    hb_audio = _hb_scan_audio(filepath)
    for t in audio:
        hb = hb_audio.get(t["track"], {})
        if hb.get("bitrate"): t["bitrate"] = hb["bitrate"]
        if hb.get("samplerate"): t["samplerate"] = hb["samplerate"]

    return {"audio": audio, "subtitles": subtitles,
            "chapters": chapters, "video": video_info}

# ── HandBrakeCLI command builder ──────────────────────────────────────────────

def build_cmd(params, output_file):
    cmd = ["HandBrakeCLI", "-i", params["input_file"], "-o", output_file]

    # If a built-in HandBrake preset is specified, use it directly via --preset
    # This overrides individual settings and uses HandBrake's own preset logic
    builtin_preset = params.get("builtin_preset", "")
    if builtin_preset:
        cmd += ["--preset", builtin_preset]
        # Still apply output format, chapter range, tags and audio/sub tracks
        fmt = params.get("container","mkv")
        cmd += ["-f", "av_mp4" if fmt == "mp4" else "av_mkv"]
        if params.get("chapter_markers", True):
            cmd += ["--markers"]
        # Chapter range
        ch_start = params.get("chapter_start")
        ch_end   = params.get("chapter_end")
        if ch_start and ch_end and str(ch_start) != str(ch_end):
            cmd += ["-c", f"{ch_start}-{ch_end}"]
        # Tags
        for tag_key, flag in [("tag_title","--title"),("tag_comment","--comment"),
                               ("tag_genre","--genre"),("tag_description","--desc")]:
            val = params.get(tag_key,"").strip()
            if val:
                cmd += [flag, val]
        # Audio tracks
        audio_tracks = params.get("audio_tracks",[])
        if audio_tracks:
            nums  = [str(t["track"]) for t in audio_tracks]
            codecs = [t.get("encoder","copy") for t in audio_tracks]
            cmd += ["-a", ",".join(nums), "-E", ",".join(codecs)]
        # Subtitle tracks
        sub_tracks = params.get("subtitle_tracks",[])
        if sub_tracks:
            nums = [str(t["track"]) for t in sub_tracks]
            cmd += ["-s", ",".join(nums)]
            burn = next((str(i+1) for i,t in enumerate(sub_tracks) if t.get("burn_in")), None)
            if burn:
                cmd += ["--subtitle-burned", burn]
        cmd += ["--json"]
        return cmd

    # Container
    fmt = params.get("container","mkv")
    cmd += ["-f", "av_mp4" if fmt == "mp4" else "av_mkv"]

    # Metadata
    if params.get("passthru_metadata", True):
        cmd += ["--keep-metadata"]

    # Chapter markers
    if params.get("chapter_markers", True):
        cmd += ["--markers"]

    # ── Dimensions ──
    res_limit = params.get("resolution_limit","")
    if res_limit == "1080p":
        cmd += ["--maxWidth","1920","--maxHeight","1080"]
    elif res_limit == "720p":
        cmd += ["--maxWidth","1280","--maxHeight","720"]
    elif res_limit == "2160p":
        cmd += ["--maxWidth","3840","--maxHeight","2160"]

    anamorphic = params.get("anamorphic","automatic")
    if anamorphic in ("automatic","auto"):
        cmd += ["--auto-anamorphic"]
    elif anamorphic == "loose":
        cmd += ["--loose-anamorphic"]
    elif anamorphic == "custom":
        cmd += ["--custom-anamorphic"]
    elif anamorphic == "none":
        cmd += ["--non-anamorphic"]

    crop = params.get("cropping","none")
    if crop == "auto":
        cmd += ["--crop-mode","auto"]
    elif crop == "none":
        cmd += ["--crop-mode","none"]
    elif crop == "custom":
        ct = params.get("crop_top",0)
        cb = params.get("crop_bottom",0)
        cl = params.get("crop_left",0)
        cr_val = params.get("crop_right",0)
        cmd += ["--crop-mode","custom","--crop",f"{ct}:{cb}:{cl}:{cr_val}"]

    rotation = params.get("rotation","")
    if rotation == "90":    cmd += ["--rotate=angle=90:hflip=0"]
    elif rotation == "180": cmd += ["--rotate=angle=180:hflip=0"]
    elif rotation == "270": cmd += ["--rotate=angle=270:hflip=0"]
    if params.get("flip_horizontal"): cmd += ["--rotate=angle=0:hflip=1"]

    if params.get("custom_width") and params.get("custom_height"):
        cmd += ["-w", str(params["custom_width"]), "-l", str(params["custom_height"])]

    if params.get("allow_upscaling"):
        cmd += ["--upscale"]

    if params.get("optimal_size"):
        cmd += ["--optimal-size"]

    # ── Filters ──
    detelecine = params.get("detelecine","off")
    if detelecine != "off":
        cmd += ["--detelecine"] if detelecine == "default" else [f"--detelecine={detelecine}"]

    interlace_detect = params.get("interlace_detection","off")
    if interlace_detect != "off":
        cmd += ["--comb-detect"] if interlace_detect == "default" else [f"--comb-detect={interlace_detect}"]

    deinterlace = params.get("deinterlace","off")
    if deinterlace != "off":
        di_preset = params.get("deinterlace_preset","default")
        if deinterlace == "decomb":
            cmd += ["--decomb"] if di_preset == "default" else [f"--decomb={di_preset}"]
        elif deinterlace == "yadif":
            cmd += ["--deinterlace"] if di_preset == "default" else [f"--deinterlace={di_preset}"]

    deblock = params.get("deblock","off")
    if deblock != "off":
        cmd += ["--deblock"] if deblock == "default" else [f"--deblock={deblock}"]

    denoise = params.get("denoise","off")
    if denoise != "off":
        dn_preset = params.get("denoise_preset","medium")
        if denoise == "nlmeans":
            cmd += [f"--nlmeans={dn_preset}"]
        elif denoise == "hqdn3d":
            cmd += [f"--hqdn3d={dn_preset}"]

    chroma_smooth = params.get("chroma_smooth","off")
    if chroma_smooth != "off":
        cmd += ["--chroma-smooth"] if chroma_smooth == "default" else [f"--chroma-smooth={chroma_smooth}"]

    sharpen = params.get("sharpen","off")
    if sharpen != "off":
        sh_preset = params.get("sharpen_preset","medium")
        if sharpen == "lapsharp":
            cmd += [f"--lapsharp={sh_preset}"]
        elif sharpen == "unsharp":
            cmd += [f"--unsharp={sh_preset}"]

    colorspace = params.get("colorspace","off")
    if colorspace != "off":
        cmd += [f"--colorspace={colorspace}"]

    if params.get("grayscale"):
        cmd += ["--grayscale"]

    # ── Video ──
    encoder_map = {
        "h265":        "x265",
        "h265_10bit":  "x265_10bit",
        "h265_12bit":  "x265_12bit",
        "h264":        "x264",
        "h264_10bit":  "x264_10bit",
        "av1":         "svt_av1",
        "av1_10bit":   "svt_av1_10bit",
        "vp8":         "vp8",
        "vp9":         "vp9",
        "vp9_10bit":   "vp9_10bit",
        "mpeg4":       "mpeg4",
        "mpeg2":       "mpeg2",
        "theora":      "theora",
        "ffv1":        "ffv1",
        "h265_nvenc":  "nvenc_h265",
        "h264_nvenc":  "nvenc_h264",
        "h265_qsv":    "qsv_h265",
        "h264_qsv":    "qsv_h264",
    }
    enc = encoder_map.get(params.get("video_encoder","h265_10bit"),"x265_10bit")
    cmd += ["-e", enc]

    # Encoders that support preset/tune/profile/level options
    enc_supports_preset = enc in {
        "x264","x264_10bit","x265","x265_10bit","x265_12bit",
        "svt_av1","svt_av1_10bit",
        "nvenc_h265","nvenc_h264","qsv_h265","qsv_h264",
    }

    fps = params.get("framerate","same")
    if fps and fps != "same":
        cmd += ["-r", str(fps)]
    fr_mode = params.get("framerate_mode","cfr")
    if fr_mode == "cfr":   cmd += ["--cfr"]
    elif fr_mode == "vfr": cmd += ["--vfr"]
    else:                  cmd += ["--pfr"]

    color_range = params.get("color_range","")
    if color_range and color_range not in ("","limited"):
        cmd += ["--color-range", color_range]

    quality_mode = params.get("quality_mode","cq")
    if quality_mode == "cq":
        cmd += ["-q", str(params.get("rf",18))]
    else:
        cmd += ["--vb", str(params.get("bitrate",1000))]
        if params.get("multi_pass"):
            cmd += ["--multi-pass"]
            if params.get("turbo_pass"):
                cmd += ["--turbo"]

    if enc_supports_preset:
        enc_preset = params.get("encoder_preset","slow")
        cmd += ["--encoder-preset", enc_preset]

        tune = params.get("tune","none")
        if tune and tune != "none":
            cmd += ["--encoder-tune", tune]

        profile = params.get("profile","auto")
        if profile and profile != "auto":
            cmd += ["--encoder-profile", profile]

        level = params.get("level","auto")
        if level and level != "auto":
            cmd += ["--encoder-level", level]

    additional = params.get("additional_options","").strip()
    if additional:
        cmd += additional.split()

    # Apply prefs (must be loaded before cpu_cores check)
    _prefs = load_prefs()

    # CPU core limit
    cpu_cores = int(params.get("cpu_cores") or _prefs.get("cpu_cores",0) or 0)
    if cpu_cores > 0:
        cmd += ["--cpu", str(cpu_cores)]

    num_previews = int(_prefs.get("num_previews", 10))
    cmd += ["--previews", str(num_previews)]
    if not _prefs.get("use_dvdnav", True):
        cmd += ["--no-dvdnav"]
    if _prefs.get("keep_duplicate_titles", False):
        cmd += ["--keep-titles"]
    min_dur = int(_prefs.get("min_title_duration", 10))
    if min_dur > 0:
        cmd += ["--min-duration", str(min_dur)]
    if _prefs.get("max_title_duration_enabled") and int(_prefs.get("max_title_duration", 0)) > 0:
        cmd += ["--max-duration", str(int(_prefs.get("max_title_duration", 0)))]

    # Use JSON output for reliable progress parsing (HB 1.11+)
    cmd += ["--json"]
    tmp_dir = _prefs.get("temp_dir", "")
    if tmp_dir and os.path.isdir(tmp_dir):
        cmd += ["--temp-dir", tmp_dir]
    verbosity = int(_prefs.get("log_verbosity", 1))
    cmd += ["--verbose", str(verbosity)]

    # ── Audio ──
    audio_tracks = params.get("audio_tracks",[])
    if audio_tracks:
        nums        = [str(t["track"]) for t in audio_tracks]
        codecs      = [t.get("encoder","copy") for t in audio_tracks]
        mixdowns    = [t.get("mixdown","none") for t in audio_tracks]
        bitrates    = [str(t.get("bitrate",320)) for t in audio_tracks]
        samplerates = [str(t.get("samplerate",0)) for t in audio_tracks]
        gains       = [str(t.get("gain",0)) for t in audio_tracks]
        names       = [t.get("name","") for t in audio_tracks]
        cmd += ["-a", ",".join(nums)]
        cmd += ["-E", ",".join(codecs)]
        if any(m != "none" for m in mixdowns):
            cmd += ["--mixdown", ",".join(m if m != "none" else "dpl2" for m in mixdowns)]
        cmd += ["-B", ",".join(bitrates)]
        if any(s != "0" for s in samplerates):
            cmd += ["-R", ",".join(samplerates)]
        if any(g != "0" for g in gains):
            cmd += ["--gain", ",".join(gains)]
        if any(n for n in names):
            cmd += ["-A", ",".join(names)]
        cmd += ["--audio-fallback", "av_aac"]

    # Auto-passthru copy mask — which codecs are allowed to pass through
    pt_map = {
        "pt-aac":"copy:aac","pt-ac3":"copy:ac3","pt-eac3":"copy:eac3",
        "pt-mp3":"copy:mp3","pt-dts":"copy:dts","pt-dtshd":"copy:dtshd",
        "pt-truehd":"copy:truehd","pt-flac":"copy:flac","pt-mp2":"copy:mp2",
        "pt-vorbis":"copy:vorbis","pt-opus":"copy:opus","pt-pcm":"copy:pcm",
    }
    enabled = [hb for ui,hb in pt_map.items() if params.get(ui, True)]
    if enabled:
        cmd += ["--audio-copy-mask", ",".join(enabled)]

    # ── Subtitles ──
    sub_tracks = params.get("subtitle_tracks",[])
    if sub_tracks:
        nums  = [str(t["track"]) for t in sub_tracks]
        names = [t.get("custom_name") or t.get("title","") for t in sub_tracks]
        burn  = next((str(i+1) for i,t in enumerate(sub_tracks) if t.get("burn_in")), None)
        cmd += ["-s", ",".join(nums)]
        if any(n for n in names):
            cmd += ["-S", ",".join(names)]
        default_sub = next((i+1 for i,t in enumerate(sub_tracks) if t.get("default")), 0)
        if default_sub:
            cmd += ["--subtitle-default", str(default_sub)]
        forced_subs = [str(i+1) for i,t in enumerate(sub_tracks) if t.get("forced")]
        if forced_subs:
            cmd += ["--subtitle-forced", forced_subs[0]]
        if burn:
            cmd += ["--subtitle-burned", burn]

    # ── Tags ──
    for tag_key, flag in [("tag_title","--title"),("tag_comment","--comment"),
                           ("tag_genre","--genre"),("tag_description","--desc")]:
        val = params.get(tag_key,"").strip()
        if val:
            cmd += [flag, val]

    # ── Chapter range ──
    ch_start = params.get("chapter_start")
    ch_end   = params.get("chapter_end")
    if ch_start and ch_end and str(ch_start) != str(ch_end):
        cmd += ["-c", f"{ch_start}-{ch_end}"]

    return cmd


def run_encode_job(job_id, params):
    prefs = load_prefs()
    input_file = params["input_file"]
    stem = Path(input_file).stem
    ext  = ".mp4" if params.get("container") == "mp4" else ".mkv"
    # Apply auto-naming template from prefs if enabled
    if prefs.get("auto_naming", True) and not params.get("save_as"):
        template = prefs.get("auto_name_template", "{source}") or "{source}"
        name = template.replace("{source}", stem)
        save_as = name + ext
    else:
        save_as = params.get("save_as") or f"{stem} (1){ext}"
    # Use .m4v for mp4 if iTunes extension pref is set
    if prefs.get("mp4_extension") and ext == ".mp4":
        save_as = Path(save_as).stem + ".m4v"
    # Auto-increment filename if output already exists
    output_file = str(Path(OUTPUT_PATH) / save_as)
    if Path(output_file).exists():
        base = save_as
        # Strip existing (N) suffix if present
        import re as _re
        base = _re.sub(r' \(\d+\)(\.[^.]+)$', r'\1', base)
        base_stem = Path(base).stem
        base_ext  = Path(base).suffix
        n = 1
        while Path(output_file).exists():
            save_as = f"{base_stem} ({n}){base_ext}"
            output_file = str(Path(OUTPUT_PATH) / save_as)
            n += 1



    with jobs_lock:
        jobs[job_id].update({"status":"encoding","progress":0,"started_at":time.time(),"eta":"","log":[],"output_file":output_file})

    # Check input file exists
    if not Path(input_file).exists():
        with jobs_lock:
            jobs[job_id].update({"status":"error","error":f"Input file not found: {input_file}"})
        return

    # Ensure output directory exists
    try:
        Path(OUTPUT_PATH).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status":"error","error":f"Cannot create output dir {OUTPUT_PATH}: {e}"})
        return

    # Check disk space if pref enabled
    _prefs3 = load_prefs()
    if _prefs3.get("pause_on_low_disk", True):
        import shutil
        free_gb = shutil.disk_usage(OUTPUT_PATH).free / (1024**3)
        threshold = float(_prefs3.get("low_disk_threshold_gb", 10))
        if free_gb < threshold:
            with jobs_lock:
                jobs[job_id].update({"status":"error","error":f"Not enough disk space: {free_gb:.1f}GB free, {threshold}GB required"})
            save_queue()
            return

    cmd = build_cmd(params, output_file)

    with jobs_lock:
        jobs[job_id]["log"].append("CMD: " + " ".join(cmd))

    try:
        # HB 1.11+ writes progress to stderr, so capture both
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)

        # Read stdout in a separate thread - HB 1.11+ --json writes Progress to stdout
        import threading as _threading, json as _json
        def read_stderr(proc, job_id):
            buffer = ""
            in_json = False
            brace_depth = 0
            for line in proc.stdout:
                line_stripped = line.rstrip()
                # JSON progress blocks start with "Progress: {"
                if line_stripped.startswith("Progress: {"):
                    buffer = "{"
                    in_json = True
                    brace_depth = 1
                    continue
                if in_json:
                    buffer += line_stripped + "\n"
                    brace_depth += line_stripped.count("{") - line_stripped.count("}")
                    if brace_depth <= 0:
                        in_json = False
                        try:
                            data = _json.loads(buffer)
                            state = data.get("State","")
                            if state == "WORKING":
                                w = data.get("Working",{})
                                pct = round(w.get("Progress",0) * 100, 1)
                                eta_secs = int(w.get("ETASeconds",0) or 0)
                                hours   = eta_secs // 3600
                                minutes = (eta_secs % 3600) // 60
                                secs    = eta_secs % 60
                                eta_str = f"{hours:02d}:{minutes:02d}:{secs:02d}" if eta_secs else ""
                                with jobs_lock:
                                    jobs[job_id]["progress"] = pct
                                    if eta_str:
                                        jobs[job_id]["eta"] = eta_str
                            elif state == "WORKDONE":
                                with jobs_lock:
                                    jobs[job_id]["progress"] = 100
                        except: pass
                        buffer = ""
                        brace_depth = 0
                    continue
                # Non-JSON lines go to log
                if line_stripped:
                    with jobs_lock:
                        jobs[job_id].setdefault("log",[])
                        jobs[job_id]["log"].append(line_stripped)
                        jobs[job_id]["log"] = jobs[job_id]["log"][-500:]
            # Log that stderr reader finished
            with jobs_lock:
                jobs[job_id].setdefault("log",[])
                jobs[job_id]["log"].append("DEBUG: stderr reader finished")
        _threading.Thread(target=read_stderr, args=(proc, job_id), daemon=True).start()

        # Also read stdout to keep pipe from blocking
        def read_stdout(proc, job_id):
            for line in proc.stderr:
                line = line.strip()
                if line:
                    with jobs_lock:
                        jobs[job_id].setdefault("log",[])
                        jobs[job_id]["log"].append(line)
                        jobs[job_id]["log"] = jobs[job_id]["log"][-500:]
        _threading.Thread(target=read_stdout, args=(proc, job_id), daemon=True).start()
        with jobs_lock:
            jobs[job_id]["pid"] = proc.pid

        # Read stdout (info/errors)
        for line in proc.stdout:
            line = line.strip()
            if not line: continue
            with jobs_lock:
                jobs[job_id].setdefault("log",[])
                jobs[job_id]["log"].append(line)
                jobs[job_id]["log"] = jobs[job_id]["log"][-500:]

        proc.wait()
        with jobs_lock:
            # -15 = SIGTERM (cancelled), -19 = SIGSTOP (paused) - not errors
            if proc.returncode in (-15, -19) or jobs[job_id].get("status") in ("cancelled","paused"):
                if jobs[job_id].get("status") not in ("cancelled","paused"):
                    jobs[job_id].update({"status":"cancelled"})
                save_queue()
                return
            log = jobs[job_id].get("log",[])
            output_exists = Path(output_file).exists() and Path(output_file).stat().st_size > 1000
            if proc.returncode == 0 and output_exists:
                output_size = Path(output_file).stat().st_size
                jobs[job_id].update({"status":"done","progress":100,"output_file":output_file,"ended_at":time.time(),"output_size":output_size})
                _prefs2 = load_prefs()
                # Save log next to movie if pref enabled
                if _prefs2.get("logs_with_movie"):
                    try:
                        log_path = str(Path(output_file).with_suffix(".log"))
                        with open(log_path, "w") as lf:
                            lf.write("\n".join(jobs[job_id].get("log", [])))
                    except Exception: pass
                # Save log to /config/EncodeLogs/ always
                try:
                    log_dir = Path("/config/EncodeLogs")
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_name = Path(output_file).stem + f" {time.strftime('%Y-%m-%d %H-%M-%S')}.log"
                    with open(log_dir / log_name, "w") as lf:
                        lf.write("\n".join(jobs[job_id].get("log", [])))
                    # Log longevity cleanup
                    longevity = _prefs2.get("log_longevity", "Month")
                    days = {"Week":7,"Month":30,"Year":365,"Indefinite":999999}.get(longevity, 30)
                    cutoff = time.time() - days * 86400
                    for old_log in log_dir.glob("*.log"):
                        if old_log.stat().st_mtime < cutoff:
                            old_log.unlink(missing_ok=True)
                except Exception: pass
                # Send file to another location if set
                if _prefs2.get("send_file_to"):
                    try:
                        dest_dir = Path(_prefs2["send_file_to"])
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        import shutil as _shutil
                        _shutil.copy2(output_file, dest_dir / Path(output_file).name)
                    except Exception as e:
                        with jobs_lock:
                            jobs[job_id]["log"].append(f"Warning: could not send file: {e}")
                if _prefs2.get("clear_completed_on_encode"):
                    jobs.pop(job_id, None)
                save_queue()
            else:
                error_lines = [l for l in log if any(w in l.lower() for w in ["error","failed","unknown option","invalid"])]
                last_error = error_lines[-1] if error_lines else (log[-1] if log else "Unknown error")
                jobs[job_id].update({"status":"error","error":f"Exit {proc.returncode}: {last_error}","ended_at":time.time()})
                save_queue()
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status":"error","error":str(e)})

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static","index.html")

# Config / locations / presets
@app.route("/api/config")
def get_config():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def set_config():
    save_config(request.json)
    return jsonify({"ok":True})

@app.route("/api/prefs")
def get_prefs():
    return jsonify(load_prefs())

@app.route("/api/prefs", methods=["POST"])
def set_prefs():
    try:
        prefs = {**PREFS_DEFAULTS, **request.json}
        save_prefs(prefs)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

RECENT_FILE = "/config/hb-recent.json"
MAX_RECENT = 20

def add_recent_file(path):
    try:
        try:
            with open(RECENT_FILE) as f:
                recent = json.load(f)
        except FileNotFoundError:
            recent = []
        # Remove if already exists
        recent = [r for r in recent if r.get("path") != path]
        # Add to front
        recent.insert(0, {
            "path": path,
            "name": Path(path).name,
            "size": format_size(Path(path).stat().st_size) if Path(path).exists() else "",
        })
        recent = recent[:MAX_RECENT]
        os.makedirs(os.path.dirname(RECENT_FILE), exist_ok=True)
        with open(RECENT_FILE, "w") as f:
            json.dump(recent, f, indent=2)
    except Exception:
        pass

def format_size(size):
    for unit in ["B","KB","MB","GB"]:
        if size < 1024: return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

@app.route("/api/recent")
def get_recent():
    try:
        with open(RECENT_FILE) as f:
            recent = json.load(f)
        # Filter to only existing files
        recent = [r for r in recent if Path(r.get("path","")).exists()]
        return jsonify({"files": recent})
    except FileNotFoundError:
        return jsonify({"files": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/session")
def get_session():
    try:
        with open(SESSION_FILE) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/session", methods=["POST"])
def set_session():
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(request.json, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Browse
@app.route("/api/browse")
def browse():
    import datetime
    path      = request.args.get("path","")
    all_files = request.args.get("all","0") == "1"
    VIDEO = {".mkv",".mp4",".avi",".mov",".m4v",".ts",".m2ts",
             ".wmv",".flv",".webm",".mpg",".mpeg",".iso",".vob"}
    if not path:
        cfg = load_config()
        return jsonify({"path":"","parent":None,"items":[],"locations":cfg.get("locations",[])})
    try:
        p = Path(path)
        if not p.exists():
            return jsonify({"error":"Not found","items":[],"locations":[]})
        items=[]
        for c in sorted(p.iterdir(), key=lambda x:(not x.is_dir(), x.name.lower())):
            if c.name.startswith("."): continue
            is_dir = c.is_dir()
            ext = c.suffix.lower()
            if not is_dir and not all_files and ext not in VIDEO: continue
            size=0
            try:
                if not is_dir: size=c.stat().st_size
            except: pass
            try:
                mdate = datetime.datetime.fromtimestamp(c.stat().st_mtime).strftime("%d %b")
            except:
                mdate=""
            ftype = "dir" if is_dir else ("video" if ext in VIDEO else "file")
            items.append({"name":c.name,"path":str(c),"type":ftype,
                           "size":size,"ext":ext,"modified":mdate})
        parent = str(p.parent) if str(p)!="/" else None
        cfg = load_config()
        return jsonify({"path":str(p),"parent":parent,"items":items,"locations":cfg.get("locations",[])})
    except PermissionError:
        return jsonify({"error":"Permission denied","items":[],"locations":[]})

@app.route("/api/mkdir", methods=["POST"])
def mkdir():
    data = request.json or {}
    path = data.get("path","").strip()
    if not path:
        return jsonify({"error":"No path"}),400
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return jsonify({"ok":True,"path":path})
    except Exception as e:
        return jsonify({"error":str(e)}),500

# Tracks
@app.route("/api/tracks")
def tracks():
    fp = request.args.get("path","")
    if not fp: return jsonify({"error":"No path"}),400
    # Track as recent file
    if Path(fp).is_file():
        add_recent_file(fp)
    return jsonify(get_track_info(fp))

# Jobs
@app.route("/api/encode", methods=["POST"])
def start_encode():
    params = request.json
    if not params or not params.get("input_file"):
        return jsonify({"error":"Missing input_file"}),400
    auto_start = params.pop("auto_start", True)  # default True for backwards compat
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "id":job_id,"status":"queued","progress":0,
            "input_file":params["input_file"],
            "filename":Path(params["input_file"]).name,
            "save_as":params.get("save_as",""),
            "preset":params.get("preset_name","Custom"),
            "created_at":time.time(),"eta":"","log":[],
            "params": params,
        }
    save_queue()
    if auto_start:
        threading.Thread(target=run_encode_job,args=(job_id,params),daemon=True).start()
    return jsonify({"job_id":job_id,"status":"queued"})

@app.route("/api/jobs/<jid>/start", methods=["POST"])
def start_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    if job["status"] != "queued": return jsonify({"error":"Job not queued"}),400
    params = job.get("params",{})
    threading.Thread(target=run_encode_job,args=(jid,params),daemon=True).start()
    return jsonify({"status":"encoding"})

@app.route("/api/jobs")
def list_jobs():
    with jobs_lock:
        result = sorted(
            [{k:v for k,v in j.items() if k not in ("log",)} for j in jobs.values()],
            key=lambda x:x.get("created_at",0),reverse=False)
    return jsonify(result)

@app.route("/api/jobs/<jid>")
def job_detail(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    return jsonify(job)

@app.route("/api/jobs/<jid>/cancel",methods=["POST"])
def cancel_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    pid = job.get("pid")
    if pid:
        try: os.kill(pid,15)
        except ProcessLookupError: pass
    with jobs_lock:
        jobs[jid]["status"]="cancelled"
    return jsonify({"status":"cancelled"})

@app.route("/api/jobs/<jid>/pause",methods=["POST"])
def pause_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, 19)  # SIGSTOP - pauses the process
            with jobs_lock:
                jobs[jid]["status"] = "paused"
                jobs[jid]["paused_at"] = time.time()
            return jsonify({"status":"paused"})
        except ProcessLookupError:
            return jsonify({"error":"Process not found"}),404
        except Exception as e:
            return jsonify({"error":str(e)}),500
    return jsonify({"error":"No PID"}),400

@app.route("/api/jobs/<jid>/resume",methods=["POST"])
def resume_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, 18)  # SIGCONT - resumes the process
            with jobs_lock:
                paused_at = jobs[jid].get("paused_at")
                if paused_at:
                    jobs[jid]["paused_duration"] = jobs[jid].get("paused_duration", 0) + (time.time() - paused_at)
                    jobs[jid].pop("paused_at", None)
                jobs[jid]["status"] = "encoding"
            return jsonify({"status":"encoding"})
        except ProcessLookupError:
            return jsonify({"error":"Process not found"}),404
        except Exception as e:
            return jsonify({"error":str(e)}),500
    return jsonify({"error":"No PID"}),400

@app.route("/api/jobs/<jid>",methods=["DELETE"])
def delete_job(jid):
    with jobs_lock:
        jobs.pop(jid,None)
    save_queue()
    return jsonify({"ok":True})

@app.route("/api/jobs/<jid>/reset", methods=["POST"])
def reset_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    with jobs_lock:
        jobs[jid]["status"] = "queued"
        jobs[jid]["progress"] = 0
        jobs[jid]["eta"] = ""
        jobs[jid]["error"] = ""
        jobs[jid]["log"] = []
        jobs[jid].pop("started_at", None)
        jobs[jid].pop("ended_at", None)
        jobs[jid].pop("output_size", None)
        jobs[jid].pop("output_file", None)
        jobs[jid].pop("pid", None)
    save_queue()
    return jsonify({"ok":True,"status":"queued"})

@app.route("/api/jobs/<jid>/log")
def job_log(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    return jsonify({"log":job.get("log",[])})

# ── Preview frame ─────────────────────────────────────────────────────────────

import hashlib, shutil

PREVIEW_CACHE_DIR = "/tmp/hb_preview_cache"
os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)

_scan_cache = {}
_scan_lock = threading.Lock()

# ── Auto-evict preview cache entries older than 7 days ───────────────────────
def _evict_preview_cache():
    while True:
        try:
            days = int(load_prefs().get("cache_evict_days", 7))
            if days > 0:
                cutoff = time.time() - (days * 24 * 3600)
                for entry in os.scandir(PREVIEW_CACHE_DIR):
                    if entry.is_dir():
                        try:
                            if entry.stat().st_mtime < cutoff:
                                shutil.rmtree(entry.path, ignore_errors=True)
                                with _scan_lock:
                                    to_del = [k for k,v in _scan_cache.items()
                                              if any(entry.path in f for f in v.get("frames",[]))]
                                    for k in to_del:
                                        del _scan_cache[k]
                        except Exception:
                            pass
        except Exception:
            pass
        time.sleep(3600)

threading.Thread(target=_evict_preview_cache, daemon=True).start()


@app.route("/api/preview-cache/clear", methods=["POST"])
def clear_preview_cache():
    try:
        shutil.rmtree(PREVIEW_CACHE_DIR, ignore_errors=True)
        os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
        with _scan_lock:
            _scan_cache.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/preview-cache/size")
def preview_cache_size():
    try:
        total = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, files in os.walk(PREVIEW_CACHE_DIR)
            for f in files
        )
        return jsonify({"bytes": total, "mb": round(total / 1024 / 1024, 1)})
    except:
        return jsonify({"bytes": 0, "mb": 0})


@app.route("/api/system/sleep", methods=["POST"])
def system_sleep():
    try:
        import subprocess
        subprocess.Popen(["systemctl", "suspend"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/shutdown", methods=["POST"])
def system_shutdown():
    try:
        import subprocess
        subprocess.Popen(["shutdown", "-h", "now"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _scan_key(filepath):
    try:
        mtime = str(os.path.getmtime(filepath))
    except:
        mtime = "0"
    return hashlib.md5(f"{filepath}|{mtime}".encode()).hexdigest()


HB_PREVIEW_BIN = "/usr/local/bin/hb_preview"

def _run_hb_scan(filepath, num_previews=10):
    """Use hb_preview (libhb) if available, otherwise fall back to ffmpeg."""
    import sys
    key = _scan_key(filepath)
    scan_dir = os.path.join(PREVIEW_CACHE_DIR, key)

    with _scan_lock:
        if filepath in _scan_cache and _scan_cache[filepath].get("frames"):
            # Validate cached frames are real content (>50KB), not black frames
            cached = _scan_cache[filepath]["frames"]
            if all(os.path.exists(f) and os.path.getsize(f) > 50000 for f in cached):
                return cached
            # Stale/black frames — rescan
            del _scan_cache[filepath]
        _scan_cache[filepath] = {"frames":[], "count":0, "total":num_previews,
                                  "scanning":True, "preview_num":0}

    os.makedirs(scan_dir, exist_ok=True)
    prefs = load_prefs()
    scale = prefs.get("scale_hd_previews", True)
    frames = []

    if os.path.exists(HB_PREVIEW_BIN):
        print(f"[preview] Using hb_preview (libhb) for {os.path.basename(filepath)}", file=sys.stderr)
        cmd = [HB_PREVIEW_BIN, filepath, scan_dir, str(num_previews)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
        # stdout: one output path per line (preview_000.jpg etc.)
        # stderr: errors and libhb log messages (discarded in production)
        written_paths = []
        for line in proc.stdout:
            line = line.strip()
            if line and os.path.isfile(line):
                written_paths.append(line)
                with _scan_lock:
                    _scan_cache[filepath]["preview_num"] = len(written_paths)
                    _scan_cache[filepath]["total"] = num_previews
        proc.wait(timeout=120)
        # Use paths reported by hb_preview; fall back to directory scan
        if written_paths:
            frames = sorted(written_paths)
        else:
            frames = sorted([
                os.path.join(scan_dir, f)
                for f in os.listdir(scan_dir)
                if f.lower().endswith((".jpg", "jpeg", ".png"))
                and os.path.getsize(os.path.join(scan_dir, f)) > 500
            ])
        print(f"[preview] hb_preview: {len(frames)}/{num_previews} frames for {os.path.basename(filepath)}", file=sys.stderr)

    if not frames:
        print(f"[preview] Falling back to ffmpeg for {os.path.basename(filepath)}", file=sys.stderr)
        frames = _generate_frames_ffmpeg(filepath, num_previews, scan_dir, scale)

    with _scan_lock:
        _scan_cache[filepath] = {"frames":frames, "count":len(frames),
                                  "total":num_previews, "scanning":False,
                                  "preview_num":len(frames)}
    return frames


def _generate_frames_ffmpeg(filepath, num_previews, scan_dir, scale=True):
    """Extract N preview frames as fast as possible, trying hardware accel first."""
    import concurrent.futures, sys

    # Get duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
        capture_output=True, text=True, timeout=20)
    duration = 120.0
    try:
        duration = float(json.loads(probe.stdout).get("format",{}).get("duration",120) or 120)
    except: pass

    timestamps = [max(5.0, min(duration*(i+0.5)/num_previews, duration-5))
                  for i in range(num_previews)]
    scale_filter = "scale='min(1280,iw)':-2" if scale else None
    frames = [None] * num_previews

    # Detect available hardware acceleration
    hw_accel = None
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"],
                          capture_output=True, text=True, timeout=5)
        accels = r.stdout.lower()
        if "vaapi" in accels:
            hw_accel = "vaapi"
        elif "cuda" in accels:
            hw_accel = "cuda"
        elif "videotoolbox" in accels:
            hw_accel = "videotoolbox"
        elif "dxva2" in accels:
            hw_accel = "dxva2"
    except: pass

    def extract_frame(idx, ts):
        out = os.path.join(scan_dir, f"frame_{idx:03d}.jpg")
        success = False

        # Try hardware accelerated first
        if hw_accel == "vaapi":
            cmd = ["nice", "-n", "10", "ffmpeg", "-y",
                   "-threads", "2",
                   "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                   "-ss", str(ts), "-i", filepath,
                   "-frames:v", "1",
                   "-vf", "hwdownload,format=nv12" + (",scale='min(1280,iw)':-2" if scale_filter else ""),
                   "-q:v", "4", "-an", "-sn", out]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                success = os.path.exists(out) and os.path.getsize(out) > 500
            except: pass

        elif hw_accel in ("cuda",):
            cmd = ["nice", "-n", "10", "ffmpeg", "-y",
                   "-threads", "2",
                   "-hwaccel", "cuda",
                   "-ss", str(ts), "-i", filepath,
                   "-frames:v", "1",
                   "-q:v", "4", "-an", "-sn", out]
            if scale_filter: cmd += ["-vf", scale_filter]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                success = os.path.exists(out) and os.path.getsize(out) > 500
            except: pass

        # Fall back to software decode with skip_frame optimisation
        if not success:
            cmd = ["nice", "-n", "10", "ffmpeg", "-y",
                   "-threads", "2",
                   "-skip_frame", "noref",
                   "-ss", str(ts), "-i", filepath,
                   "-frames:v", "1",
                   "-q:v", "4", "-an", "-sn", out]
            if scale_filter: cmd += ["-vf", scale_filter]
            try:
                subprocess.run(cmd, capture_output=True, timeout=20)
            except: pass

        if os.path.exists(out) and os.path.getsize(out) > 500:
            frames[idx] = out
        with _scan_lock:
            if filepath in _scan_cache:
                done = sum(1 for f in frames if f)
                _scan_cache[filepath]["preview_num"] = done
                _scan_cache[filepath]["total"] = num_previews

    # Single thread — VAAPI offloads decode to GPU so no benefit from parallelism
    # and multiple simultaneous ffmpeg processes would saturate the CPU
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        list(ex.map(lambda a: extract_frame(*a), enumerate(timestamps)))

    result = sorted([f for f in frames if f])
    print(f"[preview] ffmpeg ({hw_accel or 'sw'}): {len(result)}/{num_previews} frames for {os.path.basename(filepath)}", file=sys.stderr)
    return result


@app.route("/api/scan", methods=["POST"])
def scan_file():
    """Pre-scan a file and generate all preview frames. Called when user opens a source."""
    data = request.json or {}
    filepath = data.get("path", "")
    if not filepath or not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 400

    prefs = load_prefs()
    num_previews = int(prefs.get("num_previews", 10))

    # Run in background thread so the response returns immediately
    def _bg():
        _run_hb_scan(filepath, num_previews)

    with _scan_lock:
        already = _scan_cache.get(filepath, {}).get("frames")

    if not already:
        threading.Thread(target=_bg, daemon=True).start()

    return jsonify({"status": "scanning", "previews": num_previews})


@app.route("/api/scan/status")
def scan_status():
    filepath = request.args.get("path", "")
    with _scan_lock:
        info = _scan_cache.get(filepath, {})
    frames = info.get("frames", [])
    return jsonify({
        "ready":       len(frames) > 0 and not info.get("scanning", False),
        "scanning":    info.get("scanning", False),
        "count":       len(frames),
        "preview_num": info.get("preview_num", 0),
        "total":       info.get("total", 10),
    })


@app.route("/api/preview")
def preview():
    from flask import Response
    filepath = request.args.get("path", "")
    position = request.args.get("pos", "10%")   # "20%" or frame index "3"
    if not filepath:
        return jsonify({"error": "No path"}), 400

    prefs = load_prefs()
    num_previews = int(prefs.get("num_previews", 10))

    # Get or trigger scan
    with _scan_lock:
        info = _scan_cache.get(filepath, {})
    frames = info.get("frames", [])

    if not frames:
        # Not scanned yet — run synchronously for the first request, then cache
        frames = _run_hb_scan(filepath, num_previews)

    if frames:
        # Resolve position to frame index
        if str(position).endswith("%"):
            pct = float(position[:-1]) / 100.0
            idx = max(0, min(int(pct * len(frames)), len(frames) - 1))
        else:
            idx = max(0, min(int(position), len(frames) - 1))

        frame_path = frames[idx]
        if os.path.exists(frame_path):
            with open(frame_path, "rb") as f:
                img_data = f.read()
            mime = "image/png" if frame_path.lower().endswith(".png") else "image/jpeg"
            resp = Response(img_data, mimetype=mime)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

    return jsonify({"error": "Preview unavailable"}), 500

@app.route("/api/official-presets")
def official_presets():
    """Return HandBrake built-in preset list."""
    return jsonify({"categories": BUILTIN_PRESETS})

BUILTIN_PRESETS = {
    "General": [
        "Very Fast 1080p30","Very Fast 720p30","Very Fast 576p25","Very Fast 480p30",
        "Fast 1080p30","Fast 720p30","Fast 576p25","Fast 480p30",
        "HQ 1080p30 Surround","HQ 720p30 Surround","HQ 576p25 Surround","HQ 480p30 Surround",
        "Super HQ 1080p30 Surround","Super HQ 720p30 Surround","Super HQ 576p25 Surround","Super HQ 480p30 Surround",
    ],
    "Web": [
        "Creator 1080p","Creator 720p","Creator 480p",
        "Social 25 MB 5 Minutes 1080p","Social 25 MB 5 Minutes 720p","Social 25 MB 5 Minutes 480p",
        "Gmail Large 3 Minutes 720p","Gmail Medium 10 Minutes 480p","Gmail Small 3 Minutes 288p",
    ],
    "Devices": [
        "Android 1080p30","Android 720p30","Android 576p25","Android 480p30",
        "Apple 2160p60 4K HEVC Surround","Apple 1080p60 Surround","Apple 1080p30 Surround",
        "Apple 720p30 Surround","Apple 540p30 Surround","Apple 480p30 Surround",
        "Chromecast 2160p60 4K HEVC Surround","Chromecast 1080p60 Surround","Chromecast 1080p30 Surround",
        "Fire 1080p30 Surround","Fire 720p30 Surround",
        "Playstation 2160p60 4K Surround","Playstation 1080p30 Surround","Playstation 720p30",
        "Roku 2160p60 4K HEVC Surround","Roku 1080p30 Surround","Roku 720p30 Surround",
        "Xbox 1080p30 Surround",
    ],
    "Matroska": [
        "H.265 MKV 2160p60","H.265 MKV 1080p30","H.265 MKV 720p30","H.265 MKV 576p25","H.265 MKV 480p30",
        "H.264 MKV 2160p60","H.264 MKV 1080p30","H.264 MKV 720p30","H.264 MKV 576p25","H.264 MKV 480p30",
        "VP9 MKV 2160p60","VP9 MKV 1080p30","VP9 MKV 720p30","VP9 MKV 576p25","VP9 MKV 480p30",
    ],
    "MP4": [
        "H.265 MP4 2160p60","H.265 MP4 1080p30","H.265 MP4 720p30","H.265 MP4 576p25","H.265 MP4 480p30",
        "H.264 MP4 2160p60","H.264 MP4 1080p30","H.264 MP4 720p30","H.264 MP4 576p25","H.264 MP4 480p30",
    ],
    "Production": [
        "Production Max","Production Standard",
    ],
    "H.265 MKV": [
        "H.265 MKV 2160p60","H.265 MKV 1080p30","H.265 MKV 720p30",
    ],
}

# ── Import HandBrake presets ──────────────────────────────────────────────────

def _map_hb_encoder(enc):
    m={"x265":"h265","x265_10bit":"h265_10bit","x265_12bit":"h265_12bit",
       "x264":"h264","x264_10bit":"h264_10bit",
       "svt_av1":"av1","svt_av1_10bit":"av1_10bit",
       "vp8":"vp8","vp9":"vp9","vp9_10bit":"vp9_10bit",
       "mpeg4":"mpeg4","mpeg2":"mpeg2","theora":"theora","ffv1":"ffv1",
       "nvenc_h265":"h265_nvenc","nvenc_h264":"h264_nvenc",
       "qsv_h265":"h265_qsv","qsv_h264":"h264_qsv"}
    return m.get(enc,"h265_10bit")

def _map_hb_resolution(w,h):
    if not h: return ""
    if h>=2160: return "2160p"
    if h>=1080: return "1080p"
    if h>=720:  return "720p"
    return ""

def _map_hb_deinterlace(p):
    f=p.get("PictureDeinterlaceFilter","")
    if f=="decomb": return "decomb"
    if f=="yadif":  return "yadif"
    return "off"

@app.route("/api/import-presets", methods=["POST"])
def import_presets():
    try:
        hb_json = request.json
        cfg = load_config()
        imported = 0
        preset_list = hb_json.get("PresetList",[])
        for category in preset_list:
            cat_name = category.get("PresetName","Imported")
            children = category.get("ChildrenArray",[])
            if not children:
                children = [category]
            for p in children:
                name = p.get("PresetName","Unnamed")
                fps = p.get("VideoFramerate","auto")
                if fps in ("auto","same",None,""): fps = "same"
                deinterlace = p.get("PictureDeinterlaceFilter","off") or "off"
                comb_detect = p.get("PictureCombDetectPreset","off") or "off"
                audio_list = p.get("AudioList",[{}])
                first_audio = audio_list[0] if audio_list else {}
                audio_copy_mask = p.get("AudioCopyMask",[])
                pt_map = {
                    "copy:aac":"pt-aac","copy:ac3":"pt-ac3","copy:eac3":"pt-eac3",
                    "copy:mp3":"pt-mp3","copy:opus":"pt-opus","copy:dts":"pt-dts",
                    "copy:dtshd":"pt-dtshd","copy:truehd":"pt-truehd",
                    "copy:alac":"pt-alac","copy:flac":"pt-flac",
                    "copy:mp2":"pt-mp2","copy:vorbis":"pt-vorbis","copy:pcm":"pt-pcm",
                }
                crop_mode_map = {0:"none",1:"auto",2:"auto",3:"custom"}
                settings = {
                    "video_encoder":      _map_hb_encoder(p.get("VideoEncoder","x265_10bit")),
                    "rf":                 float(p.get("VideoQualitySlider",22)),
                    "encoder_preset":     p.get("VideoPreset","slow") or "slow",
                    "tune":               p.get("VideoTune","none") or "none",
                    "profile":            p.get("VideoProfile","auto") or "auto",
                    "level":              p.get("VideoLevel","auto") or "auto",
                    "framerate":          fps,
                    "framerate_mode":     p.get("VideoFramerateMode","cfr") or "cfr",
                    "color_range":        p.get("VideoColorRange","limited") or "limited",
                    "quality_mode":       "cq" if p.get("VideoQualityType",2)==2 else "bitrate",
                    "bitrate":            p.get("VideoAvgBitrate",0),
                    "two_pass":           bool(p.get("VideoMultiPass",False)),
                    "turbo_pass":         bool(p.get("VideoTurboMultiPass",False)),
                    "additional_options": p.get("VideoOptionExtra","") or "",
                    "grayscale":          bool(p.get("VideoGrayScale",False)),
                    "resolution_limit":   _map_hb_resolution(p.get("PictureWidth",0),p.get("PictureHeight",0)),
                    "anamorphic":         p.get("PicturePAR","auto") or "auto",
                    "allow_upscaling":    bool(p.get("PictureAllowUpscaling",False)),
                    "cropping":           crop_mode_map.get(p.get("PictureCropMode",0),"none"),
                    "crop_top":           p.get("PictureTopCrop",0),
                    "crop_bottom":        p.get("PictureBottomCrop",0),
                    "crop_left":          p.get("PictureLeftCrop",0),
                    "crop_right":         p.get("PictureRightCrop",0),
                    "detelecine":         p.get("PictureDetelecine","off") or "off",
                    "interlace_detection":"default" if comb_detect!="off" else "off",
                    "deinterlace":        deinterlace,
                    "deinterlace_preset": p.get("PictureDeinterlacePreset","default") or "default",
                    "deblock":            p.get("PictureDeblockPreset","off") or "off",
                    "denoise":            p.get("PictureDenoiseFilter","off") or "off",
                    "denoise_preset":     p.get("PictureDenoisePreset","medium") or "medium",
                    "chroma_smooth":      p.get("PictureChromaSmoothPreset","off") or "off",
                    "sharpen":            p.get("PictureSharpenFilter","off") or "off",
                    "sharpen_preset":     p.get("PictureSharpenPreset","medium") or "medium",
                    "colorspace":         p.get("PictureColorspacePreset","off") or "off",
                    "chapter_markers":    bool(p.get("ChapterMarkers",True)),
                    "container":          "mp4" if p.get("FileFormat","av_mkv")=="av_mp4" else "mkv",
                    "passthru_metadata":  bool(p.get("MetadataPassthru",True)),
                    # Audio selection behavior
                    "audio_language_list":     p.get("AudioLanguageList",[]),
                    "audio_sel_behavior":      p.get("AudioTrackSelectionBehavior","all"),
                    "audio_copy_mask":         audio_copy_mask,
                    "audio_fallback":          p.get("AudioEncoderFallback","opus"),
                    "audio_name_passthru":     bool(p.get("AudioTrackNamePassthru",True)),
                    "audio_secondary_encoder": bool(p.get("AudioSecondaryEncoderMode",True)),
                    "audio_autonaming":        p.get("AudioAutomaticNamingBehavior","unnamed"),
                    "passthru_enabled":        {pt_map[c]:True for c in audio_copy_mask if c in pt_map},
                    "default_audio_encoder":   first_audio.get("AudioEncoder","copy"),
                    "default_audio_bitrate":   first_audio.get("AudioBitrate",320),
                    "default_audio_mixdown":   first_audio.get("AudioMixdown","stereo"),
                    # Subtitle selection behavior
                    "sub_language_list":       p.get("SubtitleLanguageList",[]),
                    "sub_sel_behavior":        p.get("SubtitleTrackSelectionBehavior","all"),
                    "sub_burn_behavior":       p.get("SubtitleBurnBehavior","none"),
                    "sub_burn_dvd":            bool(p.get("SubtitleBurnDVDSub",False)),
                    "sub_burn_bluray":         bool(p.get("SubtitleBurnBDSub",False)),
                    "sub_foreign_scan":        bool(p.get("SubtitleAddForeignAudioSearch",False)),
                    "sub_add_if_not_match":    bool(p.get("SubtitleAddForeignAudioSubtitle",False)),
                    "sub_closed_captions":     bool(p.get("SubtitleAddCC",False)),
                    "sub_name_passthru":       bool(p.get("SubtitleTrackNamePassthru",True)),
                }
                cfg["presets"] = [x for x in cfg.get("presets",[]) if x["name"]!=name]
                cfg.setdefault("presets",[]).append({"name":name,"category":cat_name,"settings":settings})
                imported += 1
        save_config(cfg)
        return jsonify({"ok":True,"imported":imported})
    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()}),500

def _map_audio_encoder(enc):
    m={"copy":"copy","av_aac":"av_aac","ac3":"ac3","eac3":"eac3","mp3":"mp3",
       "opus":"opus","vorbis":"vorbis","flac16":"flac16","flac24":"flac24"}
    return m.get(enc,"copy")

def _map_mixdown(m):
    mx={"stereo":"stereo","mono":"mono","dpl1":"dpl1","dpl2":"dpl2",
        "5point1":"5point1","6point1":"6point1","7point1":"7point1"}
    return mx.get(m,"stereo")

# ── Power saver monitoring ─────────────────────────────────────────────────
def check_power_saver():
    """Check if system is on battery (power saver mode)."""
    try:
        import glob
        supplies = glob.glob("/sys/class/power_supply/*/status")
        for s in supplies:
            with open(s) as f:
                status = f.read().strip()
            if status == "Discharging":
                return True
    except Exception:
        pass
    return False

def power_saver_monitor():
    """Background thread: pause/resume encoding based on power saver pref."""
    was_paused_by_ps = False
    while True:
        time.sleep(10)
        try:
            prefs = load_prefs()
            if not prefs.get("pause_on_power_saver", True):
                if was_paused_by_ps:
                    # Resume any jobs we paused
                    with jobs_lock:
                        for jid, job in jobs.items():
                            if job.get("status") == "paused" and job.get("paused_by_ps"):
                                pid = job.get("pid")
                                if pid:
                                    try:
                                        os.kill(pid, 18)
                                        job["status"] = "encoding"
                                        job.pop("paused_by_ps", None)
                                    except: pass
                    was_paused_by_ps = False
                continue
            on_battery = check_power_saver()
            if on_battery and not was_paused_by_ps:
                with jobs_lock:
                    for jid, job in jobs.items():
                        if job.get("status") == "encoding":
                            pid = job.get("pid")
                            if pid:
                                try:
                                    os.kill(pid, 19)
                                    job["status"] = "paused"
                                    job["paused_by_ps"] = True
                                    job["paused_at"] = time.time()
                                except: pass
                was_paused_by_ps = True
            elif not on_battery and was_paused_by_ps:
                with jobs_lock:
                    for jid, job in jobs.items():
                        if job.get("status") == "paused" and job.get("paused_by_ps"):
                            pid = job.get("pid")
                            if pid:
                                try:
                                    os.kill(pid, 18)
                                    paused_at = job.get("paused_at")
                                    if paused_at:
                                        job["paused_duration"] = job.get("paused_duration", 0) + (time.time() - paused_at)
                                        job.pop("paused_at", None)
                                    job["status"] = "encoding"
                                    job.pop("paused_by_ps", None)
                                except: pass
                was_paused_by_ps = False
        except Exception:
            pass


# ── Auto scan folder watcher ───────────────────────────────────────────────
_known_files = set()
VIDEO_EXTENSIONS = {".mkv",".mp4",".avi",".mov",".m4v",".ts",".m2ts",".wmv",".flv",".webm",".mpg",".mpeg"}

def scan_input_folder():
    """Scan /storage for video files and return set of paths."""
    found = set()
    try:
        for root, dirs, files in os.walk("/storage"):
            for f in files:
                if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                    found.add(os.path.join(root, f))
    except Exception:
        pass
    return found

def auto_scan_monitor():
    """Background thread: watch input folder for new files."""
    global _known_files
    _known_files = scan_input_folder()
    while True:
        time.sleep(15)
        try:
            prefs = load_prefs()
            if not prefs.get("auto_scan_dvd", False):
                continue
            current = scan_input_folder()
            new_files = current - _known_files
            _known_files = current
            for fpath in sorted(new_files):
                # Check not already queued
                with jobs_lock:
                    already = any(j.get("input_file")==fpath for j in jobs.values())
                if already:
                    continue
                # Queue it with default settings
                stem = Path(fpath).stem
                ext = ".mkv"
                job_id = str(uuid.uuid4())[:8]
                params = {
                    "input_file": fpath,
                    "save_as": f"{stem}{ext}",
                    "container": "mkv",
                    "video_encoder": "h265_10bit",
                    "quality_mode": "cq",
                    "rf": 22,
                    "chapter_markers": True,
                    "audio_tracks": [],
                    "subtitle_tracks": [],
                }
                with jobs_lock:
                    jobs[job_id] = {
                        "id": job_id, "status": "queued", "progress": 0,
                        "input_file": fpath,
                        "filename": Path(fpath).name,
                        "save_as": params["save_as"],
                        "preset": "Auto",
                        "created_at": time.time(), "eta": "", "log": [],
                        "params": params,
                    }
                save_queue()
        except Exception:
            pass

load_queue()
threading.Thread(target=power_saver_monitor, daemon=True).start()
threading.Thread(target=auto_scan_monitor, daemon=True).start()

if __name__=="__main__":
    print(f"\n  HandBrake Mobile UI v2")
    print(f"  Output : {OUTPUT_PATH}")
    print(f"  Port   : {SERVER_PORT}\n")
    app.run(host="0.0.0.0",port=SERVER_PORT,debug=False,threaded=True)
