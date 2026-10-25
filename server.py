#!/usr/bin/env python3
"""
Global K-Content Scout - Local Subtitle Extraction & Proxy Server
=================================================================
브라우저 CORS 제한 및 YouTube captions.download OAuth 제약을 해결하여,
해외 유튜브 영상의 공개/자동생성 자막(전문, 타임스탬프, SRT)을 즉시 추출해주는 로컬 경량 서버입니다.

[설치 및 실행 방법]
1. 필수 라이브러리 설치:
   pip install flask flask-cors youtube-transcript-api yt-dlp

2. 서버 실행:
   python server.py

3. 브라우저에서 http://localhost:5000 접속 또는 웹 대시보드에서 '로컬 자막 서버 연동(localhost:5000)' 활성화
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
CORS(app, expose_headers=['Content-Disposition'])

DOWNLOAD_LIMIT = 250 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180
download_slot = threading.BoundedSemaphore(1)

def format_timestamp(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

def format_srt_timestamp(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

def build_srt(segments):
    lines = []
    for idx, seg in enumerate(segments, start=1):
        start = float(seg.get('start', 0))
        duration = float(seg.get('duration', 2.5))
        end = start + duration
        text = seg.get('text', '').strip()
        lines.append(f"{idx}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{text}\n")
    return "\n".join(lines)

@app.route('/')
def index():
    if os.path.exists('k_content_scout_dashboard.html'):
        return send_from_directory('.', 'k_content_scout_dashboard.html')
    return jsonify({"status": "ok", "message": "Global K-Content Scout Subtitle Server is running."})

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "k-content-scout-transcript-server", "port": 5000,
                    "video_download": True, "ffmpeg": bool(shutil.which('ffmpeg'))})


@app.route('/api/download', methods=['POST'])
def download_video():
    payload = request.get_json(silent=True)
    video_id = payload.get('videoId') if isinstance(payload, dict) else None
    if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return jsonify(status='error', error='올바른 YouTube 영상 ID가 필요합니다.'), 400
    if not download_slot.acquire(blocking=False):
        return jsonify(status='error', error='다른 영상을 준비 중입니다. 완료 후 다시 시도하세요.'), 409

    temporary = None
    handed_off = False
    try:
        temporary = tempfile.TemporaryDirectory(prefix='k-scout-video-')
        output = Path(temporary.name) / 'video.%(ext)s'
        # Without FFmpeg, choose an existing combined audio/video stream.
        video_format = ('bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b'
                        if shutil.which('ffmpeg') else 'b[ext=mp4]/b')
        command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
                   '--no-progress', '--no-warnings', '--socket-timeout', '15', '--retries', '1',
                   '--fragment-retries', '1', '--max-filesize', str(DOWNLOAD_LIMIT),
                   '--format', video_format, '--merge-output-format', 'mp4',
                   '--match-filter', '!is_live', '--output', str(output),
                   '--', f'https://www.youtube.com/watch?v={video_id}']
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=DOWNLOAD_TIMEOUT)
        if result.returncode:
            detail = (result.stderr or result.stdout or 'YouTube 응답 오류').strip()[-1200:]
            return jsonify(status='error', error=f'영상 다운로드 실패: {detail}'), 502

        candidates = [p for p in Path(temporary.name).iterdir()
                      if p.is_file() and p.name in ('video.mp4', 'video.webm', 'video.mkv', 'video.3gp', 'video.flv')]
        if len(candidates) != 1 or candidates[0].stat().st_size == 0:
            return jsonify(status='error', error='저장 가능한 영상이 없습니다. 라이브 방송, 용량 제한 또는 지원 형식을 확인하세요.'), 422
        path = candidates[0]
        if path.stat().st_size > DOWNLOAD_LIMIT:
            return jsonify(status='error', error='영상 용량이 250MB를 초과했습니다.'), 413
        response = send_file(path, as_attachment=True, download_name=f'youtube-{video_id}{path.suffix}',
                             conditional=False, max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        # Keep the temporary file until the WSGI server finishes streaming it,
        # then close the file before cleanup (including on Windows).
        response.direct_passthrough = False
        response.call_on_close(temporary.cleanup)
        handed_off = True
        return response
    except subprocess.TimeoutExpired:
        return jsonify(status='error', error='영상 준비 시간이 3분을 초과했습니다. 더 짧은 영상으로 다시 시도하세요.'), 504
    except OSError:
        return jsonify(status='error', error='파일을 저장하거나 다운로드 도구를 실행하지 못했습니다. 디스크 공간과 Python 설치를 확인하세요.'), 500
    finally:
        if temporary is not None and not handed_off:
            temporary.cleanup()
        download_slot.release()

@app.route('/api/transcript', methods=['GET'])
def get_transcript():
    video_id = request.args.get('videoId', '').strip()
    preferred_lang = request.args.get('lang', 'en').strip()

    if not video_id:
        return jsonify({"status": "error", "error": "videoId 파라미터가 필요합니다."}), 400

    # 1차 시도: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)

        langs_priority = [preferred_lang, 'en', 'ja', 'es', 'fr', 'de', 'pt', 'ru', 'id', 'vi', 'th', 'ko']
        # 중복 제거
        langs_priority = list(dict.fromkeys(langs_priority))

        selected_transcript = None
        available_langs = []

        for tr in transcript_list:
            available_langs.append({
                "language": tr.language,
                "language_code": tr.language_code,
                "is_generated": tr.is_generated
            })

        try:
            selected_transcript = transcript_list.find_transcript(langs_priority)
        except Exception:
            # 우선순위 언어가 없으면 첫 번째 트랙 선택
            for tr in transcript_list:
                selected_transcript = tr
                break

        if selected_transcript is not None:
            raw_entries = selected_transcript.fetch()
            segments = []
            for item in raw_entries:
                # youtube-transcript-api 버전에 따라 dict 또는 객체 지원
                if isinstance(item, dict):
                    start = float(item.get('start', 0))
                    duration = float(item.get('duration', 2.0))
                    text = str(item.get('text', '')).replace('\n', ' ').strip()
                else:
                    start = float(getattr(item, 'start', 0))
                    duration = float(getattr(item, 'duration', 2.0))
                    text = str(getattr(item, 'text', '')).replace('\n', ' ').strip()

                if text:
                    segments.append({
                        "start": round(start, 2),
                        "time": format_timestamp(start),
                        "duration": round(duration, 2),
                        "text": text
                    })

            full_text = " ".join([s["text"] for s in segments])
            srt_content = build_srt(segments)

            return jsonify({
                "status": "ok",
                "source": "youtube-transcript-api",
                "videoId": video_id,
                "language": selected_transcript.language,
                "language_code": selected_transcript.language_code,
                "is_generated": selected_transcript.is_generated,
                "available_languages": available_langs,
                "segments": segments,
                "full_text": full_text,
                "srt": srt_content
            })
    except Exception as e_ytt:
        ytt_error = str(e_ytt)
    else:
        ytt_error = "No transcript found via youtube-transcript-api"

    # 2차 폴백: yt-dlp로 자막 URL 추출 및 파싱
    try:
        import yt_dlp
        import urllib.request
        import json

        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'quiet': True,
            'no_warnings': True,
        }
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            subtitles = info.get('subtitles') or {}
            auto_captions = info.get('automatic_captions') or {}

            all_tracks = {}
            all_tracks.update(auto_captions)
            all_tracks.update(subtitles)

            if not all_tracks:
                return jsonify({
                    "status": "error",
                    "error": f"해당 영상({video_id})에 공개된 자막 트랙이 없습니다. ({ytt_error})"
                }), 404

            chosen_lang = preferred_lang if preferred_lang in all_tracks else ('en' if 'en' in all_tracks else next(iter(all_tracks.keys())))
            formats = all_tracks[chosen_lang]
            json3_fmt = next((f for f in formats if f.get('ext') == 'json3'), formats[0])
            sub_url = json3_fmt.get('url')

            req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_sub = resp.read().decode('utf-8', errors='ignore')

            segments = []
            if json3_fmt.get('ext') == 'json3':
                data = json.loads(raw_sub)
                for ev in data.get('events', []):
                    segs = ev.get('segs')
                    if not segs:
                        continue
                    text = "".join(s.get('utf8', '') for s in segs).replace('\n', ' ').strip()
                    if not text:
                        continue
                    start_ms = ev.get('tStartMs', 0)
                    dur_ms = ev.get('dDurationMs', 2000)
                    start_sec = start_ms / 1000.0
                    segments.append({
                        "start": round(start_sec, 2),
                        "time": format_timestamp(start_sec),
                        "duration": round(dur_ms / 1000.0, 2),
                        "text": text
                    })
            else:
                # 간단한 XML/VTT 태그 제거 폴백
                clean_lines = [re.sub(r'<[^>]+>', '', line).strip() for line in raw_sub.splitlines()]
                clean_lines = [l for l in clean_lines if l and not re.match(r'^(WEBVTT|\d+$|\d{2}:\d{2})', l)]
                for i, line in enumerate(clean_lines):
                    segments.append({
                        "start": i * 3.0,
                        "time": format_timestamp(i * 3.0),
                        "duration": 3.0,
                        "text": line
                    })

            full_text = " ".join([s["text"] for s in segments])
            srt_content = build_srt(segments)

            return jsonify({
                "status": "ok",
                "source": "yt-dlp",
                "videoId": video_id,
                "language": chosen_lang,
                "language_code": chosen_lang,
                "is_generated": chosen_lang not in subtitles,
                "available_languages": [{"language": k, "language_code": k, "is_generated": k not in subtitles} for k in all_tracks.keys()],
                "segments": segments,
                "full_text": full_text,
                "srt": srt_content
            })
    except Exception as e_ydl:
        return jsonify({
            "status": "error",
            "error": f"자막 추출 실패: {ytt_error} / yt-dlp 오류: {str(e_ydl)}"
        }), 500


if __name__ == '__main__':
    print("=" * 65)
    print("🚀 Global K-Content Scout 로컬 자막 추출 서버가 시작되었습니다!")
    print("👉 대시보드 주소: http://localhost:5000")
    print("👉 자막 API 엔드포인트: http://localhost:5000/api/transcript?videoId=<VIDEO_ID>")
    print("=" * 65)
    app.run(host='127.0.0.1', port=5000, debug=False)
