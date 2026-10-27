"""Local, resumable-by-job-id Shorts production. No upload/publishing step."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

bp = Blueprint('automation', __name__)
lock = threading.Lock()
active_process = None
MAX_SOURCE_BYTES = 250 * 1024 * 1024
REACTIONS = ['amazing', 'incredible', 'impressed', 'delicious', 'love it', 'beautiful', 'wow', 'surprised', 'cannot believe', "can't believe", '놀라', '대박', '감동', '맛있', '최고', '멋지', '신기', 'すごい', '美味', '놀랍']
TOPICS = ['korea', 'korean', 'seoul', 'kimchi', '한국', '서울', '한식', '김치', '韓国', '韩国']
NEGATIONS = ['not good', 'not amazing', 'not impressed', 'terrible', 'disappoint', 'hate', '별로', '실망', '최악', '싫어']


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def update(job_dir, state, stage, progress, **extra):
    path = job_dir / 'status.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    previous.update(status=state, stage=stage, progress=progress, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
    write_json(path, previous)


def run_command(args, timeout, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or '외부 도구 실행 실패')[-1200:])
    return result.stdout


def probe(path):
    data = json.loads(run_command(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 30))
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('유효한 영상 길이를 확인하지 못했습니다.')
    return duration, data['streams']


def normalize_segments(raw):
    result = []
    for item in raw:
        get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
        try:
            start, duration = float(get('start', -1)), float(get('duration', 0))
            text = str(get('text', '')).strip()
            if not text or not math.isfinite(start + duration) or start < 0 or duration <= 0:
                continue
            result.append({'start': start, 'duration': duration, 'text': text})
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda item: item['start'])


def fetch_primary_transcript(video_id, language):
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    class BoundedSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault('timeout', 20)
            return super().request(*args, **kwargs)
    with BoundedSession() as session:
        tracks = YouTubeTranscriptApi(http_client=session).list(video_id)
        languages = list(dict.fromkeys([language, 'en', 'ko', 'ja', 'es', 'fr', 'de']))
        try:
            chosen = tracks.find_transcript(languages)
        except Exception:
            chosen = next(iter(tracks))
        segments = normalize_segments(chosen.fetch())
        if not segments:
            raise ValueError('실제 타임스탬프가 있는 자막을 찾지 못했습니다.')
        return {'language': chosen.language_code, 'is_generated': chosen.is_generated, 'segments': segments}


def fetch_transcript(video_id, language):
    try:
        return fetch_primary_transcript(video_id, language)
    except Exception as primary_error:
        # Only timed JSON3 captions are accepted for editing; never invent timing.
        try:
            with tempfile.TemporaryDirectory(prefix='kscout-captions-') as temporary:
                folder = Path(temporary)
                languages = list(dict.fromkeys([language, 'en', 'ko']))
                run_command([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
                             '--skip-download', '--write-subs', '--write-auto-subs', '--sub-format', 'json3',
                             '--sub-langs', ','.join(languages), '--socket-timeout', '15', '--retries', '1',
                             '--extractor-retries', '1', '-o', str(folder / 'captions.%(ext)s'),
                             '--', f'https://www.youtube.com/watch?v={video_id}'], 120)
                for code in languages:
                    path = folder / f'captions.{code}.json3'
                    if not path.exists():
                        continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                    raw = [{'start': event.get('tStartMs', -1000) / 1000,
                            'duration': event.get('dDurationMs', 0) / 1000,
                            'text': ''.join(s.get('utf8', '') for s in event.get('segs', []))}
                           for event in data.get('events', [])]
                    segments = normalize_segments(raw)
                    if segments:
                        return {'language': code, 'is_generated': None, 'segments': segments, 'source': 'yt-dlp JSON3'}
                raise ValueError('실제 타임스탬프가 있는 JSON3 자막이 없습니다.')
        except Exception as fallback_error:
            raise RuntimeError(f'자막 추출 실패: {str(primary_error)[-400:]} / {str(fallback_error)[-600:]}') from fallback_error


def download_source(video_id, folder):
    run_command([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-progress',
                 '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1', '--fragment-retries', '1',
                 '--max-filesize', str(MAX_SOURCE_BYTES), '--match-filter', '!is_live & duration <= 3600',
                 '-f', 'bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/b[height<=1080]',
                 '--merge-output-format', 'mp4', '-o', str(folder / 'original.%(ext)s'),
                 '--', f'https://www.youtube.com/watch?v={video_id}'], 240)
    paths = [p for p in folder.glob('original.*') if p.suffix in ('.mp4', '.webm', '.mkv')]
    if len(paths) != 1 or not 0 < paths[0].stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError('원본이 없거나 250MB 용량 제한을 초과했습니다.')
    return paths[0]


def choose_clips(segments, duration, count):
    """Consecutive original speech only; scores are suggestions, not semantic truth."""
    candidates = []
    for i, cue in enumerate(segments):
        lower = cue['text'].lower()
        if not any(word in lower for word in REACTIONS):
            continue
        start = max(0, segments[max(0, i - 2)]['start'])
        limit = min(duration, start + 59.0)
        cues = [s for s in segments if s['start'] >= start and s['start'] + s['duration'] <= limit + 0.001]
        if not cues:
            continue
        end = min(limit, max(s['start'] + s['duration'] for s in cues))
        text = ' '.join(s['text'] for s in cues)
        lowered = text.lower()
        reactions = [word for word in REACTIONS if word in lowered]
        topics = [word for word in TOPICS if word in lowered]
        negatives = [word for word in NEGATIONS if word in lowered]
        coverage = sum(s['duration'] for s in cues) / max(1, end - start)
        if end - start < 10 or coverage < 0.2 or not topics or negatives:
            continue
        score = len(reactions) * 3 + len(topics) * 2
        candidates.append({'start': round(start, 3), 'end': round(end, 3), 'score': score,
                           'reason': '자막에 한국 관련 표현과 감탄·긍정 반응이 함께 등장',
                           'matched_terms': reactions + topics, 'text': text})
    selected = []
    for clip in sorted(candidates, key=lambda c: (-c['score'], c['start'])):
        if all(clip['end'] <= old['start'] or clip['start'] >= old['end'] for old in selected):
            selected.append(clip)
            if len(selected) == count:
                break
    return selected


def srt_time(value):
    millis = max(0, round(value * 1000))
    seconds, ms = divmod(millis, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def srt_text(segments, start=0, end=float('inf')):
    rows = []
    for cue in segments:
        left, right = max(start, cue['start']), min(end, cue['start'] + cue['duration'])
        if right <= left:
            continue
        # Keep speech text; prevent subtitle styling commands from changing the layout.
        text = re.sub(r'<[^>]*>', '', cue['text']).replace('{', '｛').replace('}', '｝')
        rows.append(f'{len(rows)+1}\n{srt_time(left-start)} --> {srt_time(right-start)}\n{text}\n')
    return '\n'.join(rows)


def render_clip(source, segments, clip, folder, index):
    stem = f'short-{index:02}'
    subtitle = folder / f'{stem}.srt'
    subtitle.write_text(srt_text(segments, clip['start'], clip['end']), encoding='utf-8')
    temp = folder / f'{stem}.partial.mp4'
    filters = ("[0:v]split=2[bg][fg];[bg]scale=720:1280:force_original_aspect_ratio=increase,"
               "crop=720:1280,boxblur=20:1[blur];[fg]scale=720:1280:force_original_aspect_ratio=decrease[front];"
               "[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1,"
               f"subtitles={subtitle.name}:force_style='FontName=Noto Sans CJK KR,FontSize=18,Outline=2,MarginV=60'[out]")
    run_command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-ss', str(clip['start']),
                 '-i', str(source.resolve()), '-t', str(clip['end'] - clip['start']), '-filter_complex_threads', '1',
                 '-filter_complex', filters, '-map', '[out]', '-map', '0:a?', '-c:v', 'libx264',
                 '-threads', '2', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p', '-r', '30',
                 '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(temp.resolve())], 600, cwd=folder)
    duration, streams = probe(temp)
    video = next((s for s in streams if s['codec_type'] == 'video'), {})
    if duration > 60 or video.get('width') != 720 or video.get('height') != 1280:
        raise ValueError('렌더 결과의 길이 또는 세로 해상도 검증에 실패했습니다.')
    output = folder / f'{stem}.mp4'
    temp.replace(output)
    return {**clip, 'file': output.name, 'srt': subtitle.name, 'duration': round(duration, 3),
            'width': 720, 'height': 1280, 'has_audio': any(s['codec_type'] == 'audio' for s in streams)}


def produce(job_dir):
    payload = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    report = {'job_id': job_dir.name, 'status': 'failed', 'created_at': datetime.now(timezone.utc).isoformat(),
              'selection_method': '자막 키워드·긍정 반응 규칙 기반, 연속 구간 추출',
              'requested_clips': payload['clip_count'], 'attempts': [], 'clips': [], 'artifacts': [],
              'notes': ['자동 번역·재녹음 없이 원본 음성과 추출된 언어의 자막을 사용합니다.',
                        '감정·국적·맥락을 확정하는 분석이 아닙니다. 저장된 구간과 원문을 검토하세요.']}
    try:
        selected = None
        for candidate in payload['candidates']:
            folder = job_dir / candidate['videoId']
            folder.mkdir(exist_ok=True)
            try:
                update(job_dir, 'running', '자막 확보', 10, current_video=candidate['videoId'])
                transcript = fetch_transcript(candidate['videoId'], payload['language'])
                write_json(folder / 'transcript.json', transcript)
                (folder / 'transcript.srt').write_text(srt_text(transcript['segments']), encoding='utf-8')
                update(job_dir, 'running', '원본 영상 다운로드', 25)
                source = download_source(candidate['videoId'], folder)
                duration, _ = probe(source)
                clips = choose_clips(transcript['segments'], duration, payload['clip_count'])
                if not clips:
                    raise ValueError('한국 관련 긍정 반응을 포함하는 10~59초 연속 자막 구간을 찾지 못했습니다.')
                selected = (candidate, folder, transcript, source, clips)
                report['attempts'].append({'videoId': candidate['videoId'], 'status': 'selected'})
                break
            except Exception as error:
                report['attempts'].append({'videoId': candidate['videoId'], 'status': 'skipped', 'error': str(error)[-1200:]})
                shutil.rmtree(folder, ignore_errors=True)
        if selected is None:
            raise ValueError('후보들의 자막·원본·반응 구간을 확보하지 못했습니다. 시도 내역을 확인하세요.')
        candidate, folder, transcript, source, clips = selected
        report['source'] = {**candidate, 'url': f"https://www.youtube.com/watch?v={candidate['videoId']}",
                            'language': transcript['language'], 'original': f'{folder.name}/{source.name}'}
        report['artifacts'] = [report['source']['original'], f'{folder.name}/transcript.json', f'{folder.name}/transcript.srt']
        for index, clip in enumerate(clips, 1):
            update(job_dir, 'running', f'쇼츠 편집 {index}/{len(clips)}', 40 + round(45 * (index-1) / len(clips)))
            rendered = render_clip(source, transcript['segments'], clip, folder, index)
            rendered['file'] = f"{folder.name}/{rendered['file']}"
            rendered['srt'] = f"{folder.name}/{rendered['srt']}"
            report['clips'].append(rendered)
        report['status'] = 'completed'
        report['saved_directory'] = str(job_dir.resolve())
        for clip in report['clips']:
            report['artifacts'].extend([clip['file'], clip['srt']])
    except Exception as error:
        report['error'] = str(error)[-1600:]
        # A failed job may contain usable, verified clips. Expose them explicitly.
        for clip in report['clips']:
            report['artifacts'].extend([clip['file'], clip['srt']])
    report['artifacts'].extend(['report.json', 'report.txt'])
    if report['status'] == 'completed':
        report['artifacts'].append('shorts-results.zip')
    write_json(job_dir / 'report.json', report)
    (job_dir / 'report.txt').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if report['status'] == 'completed':
        try:
            with zipfile.ZipFile(job_dir / 'shorts-results.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in report['artifacts']:
                    if name not in ('shorts-results.zip', report['source']['original']):
                        archive.write(job_dir / name, name)
        except Exception as error:
            report['status'] = 'failed'
            report['error'] = f'영상은 저장했지만 ZIP 저장에 실패했습니다: {error}'
            report['artifacts'].remove('shorts-results.zip')
            write_json(job_dir / 'report.json', report)
            (job_dir / 'report.txt').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    update(job_dir, report['status'], '저장 완료' if report['status'] == 'completed' else '제작 실패', 100, report=report)
    return report


def output_root():
    return Path(current_app.config.get('AUTOMATION_OUTPUT_DIR', Path(__file__).resolve().parent / 'outputs'))


def validate_payload(data):
    if not isinstance(data, dict) or not isinstance(data.get('candidates'), list) or not 1 <= len(data['candidates']) <= 3:
        raise ValueError('영상 후보를 1~3개 전달해야 합니다.')
    count = data.get('clip_count', 3)
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError('쇼츠 개수는 1~3개입니다.')
    language = data.get('language', 'en')
    if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z-]{2,16}', language):
        raise ValueError('올바른 자막 언어를 선택하세요.')
    candidates = []
    for item in data['candidates']:
        if not isinstance(item, dict) or not isinstance(item.get('videoId'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', item['videoId']):
            raise ValueError('올바른 영상 ID가 필요합니다.')
        candidates.append({'videoId': item['videoId'], 'title': str(item.get('title', ''))[:300],
                           'selection_reason': str(item.get('selection_reason', '직접 선택'))[:500]})
    return {'candidates': candidates, 'clip_count': count, 'language': language}


@bp.route('/api/automation', methods=['POST'])
def start_job():
    global active_process
    try:
        payload = validate_payload(request.get_json(silent=True))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        return jsonify(error='자동 편집에는 FFmpeg와 ffprobe 설치 후 서버 재시작이 필요합니다.'), 503
    try:
        filters = run_command(['ffmpeg', '-hide_banner', '-filters'], 15)
    except Exception as error:
        return jsonify(error=f'FFmpeg 실행 확인 실패: {str(error)[-300:]}'), 503
    if 'subtitles' not in filters:
        return jsonify(error='자막을 넣으려면 libass subtitles 필터가 포함된 FFmpeg가 필요합니다.'), 503
    with lock:
        if active_process and active_process.poll() is None:
            return jsonify(error='이미 자동 제작 중입니다. 현재 작업이 끝난 뒤 다시 실행하세요.'), 409
        folder = output_root() / uuid.uuid4().hex
        folder.mkdir(parents=True)
        write_json(folder / 'request.json', payload)
        update(folder, 'queued', '제작 대기', 0)
        try:
            with (folder / 'worker.log').open('w', encoding='utf-8') as log:
                active_process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run', str(folder.resolve())],
                                                  stdout=log, stderr=log, cwd=Path(__file__).resolve().parent)
        except OSError as error:
            update(folder, 'failed', '작업 시작 실패', 100, report={'error': str(error), 'clips': [], 'artifacts': []})
            return jsonify(error='편집 작업 프로세스를 시작하지 못했습니다.'), 500
    return jsonify(job_id=folder.name, status='queued'), 202


@bp.route('/api/automation/<job_id>', methods=['GET'])
def job_status(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return jsonify(error='작업을 찾을 수 없습니다.'), 404
    path = output_root() / job_id / 'status.json'
    if not path.exists():
        return jsonify(error='작업을 찾을 수 없습니다.'), 404
    return jsonify(json.loads(path.read_text(encoding='utf-8')))


@bp.route('/api/automation/<job_id>/files/<path:filename>', methods=['GET'])
def job_artifact(job_id, filename):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return jsonify(error='파일을 찾을 수 없습니다.'), 404
    folder = output_root() / job_id
    report_path = folder / 'report.json'
    if not report_path.exists():
        return jsonify(error='아직 결과가 저장되지 않았습니다.'), 404
    report = json.loads(report_path.read_text(encoding='utf-8'))
    path = (folder / filename).resolve()
    if filename not in report['artifacts'] or not path.is_relative_to(folder.resolve()) or not path.is_file():
        return jsonify(error='파일을 찾을 수 없습니다.'), 404
    return send_file(path, as_attachment=True, download_name=path.name, max_age=0)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--run':
        produce(Path(sys.argv[2]))
