import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import automation
import server


class AutomationTests(unittest.TestCase):
    def test_segment_normalization_preserves_real_times(self):
        normalized = automation.normalize_segments([{'start': 2.25, 'duration': 1.5, 'text': 'Korea wow'},
                                                     {'start': -1, 'duration': 2, 'text': 'bad'},
                                                     {'start': float('nan'), 'duration': 2, 'text': 'bad'}])
        self.assertEqual(normalized, [{'start': 2.25, 'duration': 1.5, 'text': 'Korea wow'}])
        self.assertIn('00:00:00,250 --> 00:00:01,000', automation.srt_text(normalized, 2, 3))

    def test_relevant_nonoverlapping_clips_below_one_minute(self):
        segments = [{'start': i * 5, 'duration': 5, 'text': 'Korea is amazing, I love it'} for i in range(50)]
        clips = automation.choose_clips(segments, 250, 3)
        self.assertEqual(len(clips), 3)
        for clip in clips:
            self.assertLessEqual(clip['end'] - clip['start'], 59)
            self.assertGreaterEqual(clip['end'] - clip['start'], 10)
        for left, right in zip(clips, clips[1:]):
            self.assertLessEqual(left['end'], right['start'])

    def test_negative_or_unrelated_or_short_speech_is_not_selected(self):
        for text in ['Korea is not amazing, terrible', 'Korea ordinary timetable', 'Football is amazing']:
            self.assertEqual(automation.choose_clips([{'start': 0, 'duration': 20, 'text': text}], 20, 3), [])
        self.assertEqual(automation.choose_clips([{'start': 0, 'duration': 2, 'text': 'Korea amazing'}], 2, 3), [])

    def test_bad_requests_and_missing_ffmpeg(self):
        client = server.app.test_client()
        for data in [{}, {'candidates': [{'videoId': '../escape'}]}, {'candidates': [{'videoId': 'abc123def45'}], 'clip_count': 9}]:
            self.assertEqual(client.post('/api/automation', json=data).status_code, 400)
        with patch.object(automation.shutil, 'which', return_value=None):
            self.assertEqual(client.post('/api/automation', json={'candidates':[{'videoId':'abc123def45'}]}).status_code, 503)

    def test_jobs_are_persisted_and_private_files_cannot_be_downloaded(self):
        with tempfile.TemporaryDirectory() as folder:
            server.app.config['AUTOMATION_OUTPUT_DIR'] = folder
            client = server.app.test_client()
            fake_process = Mock(); fake_process.poll.return_value = None
            try:
                with patch.object(automation, 'active_process', None), patch.object(automation.subprocess, 'Popen', return_value=fake_process), patch.object(automation, 'run_command', return_value='subtitles'), patch.object(automation.shutil, 'which', return_value='/tool'):
                    response = client.post('/api/automation', json={'candidates':[{'videoId':'abc123def45'}]})
                    self.assertEqual(response.status_code, 202)
                    job = response.json['job_id']
                    self.assertEqual(client.get('/api/automation/'+job).json['status'], 'queued')
                    self.assertEqual(client.post('/api/automation', json={'candidates':[{'videoId':'abc123def45'}]}).status_code, 409)
                root = Path(folder)/job
                automation.write_json(root/'report.json', {'artifacts':['report.json']})
                response = client.get(f'/api/automation/{job}/files/report.json')
                self.assertEqual(response.status_code, 200); response.close()
                self.assertEqual(client.get(f'/api/automation/{job}/files/request.json').status_code, 404)
                self.assertEqual(client.get(f'/api/automation/{job}/files/../request.json').status_code, 404)
                self.assertEqual(client.get(f'/static/outputs/{job}/request.json').status_code, 404)
            finally:
                server.app.config.pop('AUTOMATION_OUTPUT_DIR', None)

    def test_failed_candidates_produce_failure_report_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation.write_json(root/'request.json', {'candidates':[{'videoId':'abc123def45'}], 'clip_count':3, 'language':'en'})
            with patch.object(automation, 'fetch_transcript', side_effect=RuntimeError('no captions')):
                result = automation.produce(root)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['clips'], [])
            self.assertIn('no captions', result['attempts'][0]['error'])
            self.assertTrue((root/'report.json').exists())
            self.assertFalse((root/'shorts-results.zip').exists())

    def test_json3_fallback_keeps_original_timestamps(self):
        def write_captions(command, timeout):
            folder = Path(command[command.index('-o') + 1]).parent
            (folder/'captions.en.json3').write_text(json.dumps({'events':[{'tStartMs':1250,'dDurationMs':2750,'segs':[{'utf8':'Korea amazing'}]}]}))
        with patch.object(automation, 'fetch_primary_transcript', side_effect=RuntimeError('primary failed')), patch.object(automation, 'run_command', side_effect=write_captions):
            result = automation.fetch_transcript('abc123def45','en')
        self.assertEqual(result['segments'][0]['start'],1.25)
        self.assertEqual(result['segments'][0]['duration'],2.75)

    def test_render_failure_retains_source_and_transcript_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation.write_json(root/'request.json', {'candidates':[{'videoId':'abc123def45'}], 'clip_count':1, 'language':'en'})
            def download(video, folder):
                path = folder/'original.mp4';path.write_bytes(b'fixture');return path
            transcript = {'language':'en','segments':[{'start':0,'duration':20,'text':'Korea is amazing'}]}
            with patch.object(automation, 'fetch_transcript', return_value=transcript), patch.object(automation, 'download_source', side_effect=download), patch.object(automation, 'probe', return_value=(20,[])), patch.object(automation, 'render_clip', side_effect=RuntimeError('renderer failed')):
                report = automation.produce(root)
            self.assertEqual(report['status'],'failed')
            self.assertIn('abc123def45/original.mp4',report['artifacts'])
            self.assertIn('abc123def45/transcript.srt',report['artifacts'])
            self.assertIn('renderer failed',report['error'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_render_pipeline_with_fixture_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root/'fixture.mp4'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=s=160x90:r=15:d=16',
                            '-f','lavfi','-i','sine=frequency=440:duration=16','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(fixture)],check=True,timeout=30)
            job = root/'job';job.mkdir()
            automation.write_json(job/'request.json', {'candidates':[{'videoId':'abc123def45','title':'Test Korean food'}], 'clip_count':3, 'language':'en'})
            transcript = {'language':'en','is_generated':False,'segments':[{'start':i*4,'duration':4,'text':'Korean food is amazing!'} for i in range(4)]}
            def copy_source(video, folder):
                return Path(shutil.copyfile(fixture, folder/'original.mp4'))
            with patch.object(automation, 'fetch_transcript', return_value=transcript), patch.object(automation, 'download_source', side_effect=copy_source):
                result = automation.produce(job)
            self.assertEqual(result['status'], 'completed', result.get('error'))
            self.assertEqual(len(result['clips']), 1)
            clip = result['clips'][0]
            duration, streams = automation.probe(job/clip['file'])
            self.assertLess(duration, 60)
            self.assertEqual(next(s for s in streams if s['codec_type']=='video')['width'],720)
            self.assertTrue(clip['has_audio'])
            self.assertTrue((job/'shorts-results.zip').exists())
            self.assertEqual(json.loads((job/'status.json').read_text())['status'],'completed')
            # Retain one real render outside the repository for visual inspection.
            shutil.copyfile(job/clip['file'], '/workspace/automation-render-check.mp4')


if __name__ == '__main__':
    unittest.main()
