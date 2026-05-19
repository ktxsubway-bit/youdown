# -*- coding: utf-8 -*-
"""
Python Flask - YouTube Downloader Backend API (Render Deployment Version)
* 역할:
  1. yt-dlp 라이브러리를 통해 유튜브의 다양한 해상도 주소와 보안 서명(Signature)을 완벽히 우회 추출합니다.
  2. Render의 메모리 제한(512MB)을 초과하지 않도록 청크(Chunk) 단위 스트리밍 파이프라인을 제공합니다.
  3. 로컬 테스트 및 배포된 프론트엔드 환경에서 CORS 제약 없이 파일을 내려받을 수 있도록 응답 헤더를 제어합니다.
"""

import os
import re
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
# 프론트엔드에서 API 서버로 안전하게 교차 출처 요청(CORS)을 보낼 수 있도록 허용
CORS(app)

def extract_video_id(url):
    """다양한 형식의 유튜브 URL에서 11자리 고유 Video ID를 추출하는 정규식 함수"""
    reg_exp = r'^.*(youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=|\&v=)([^#\&\?]*).*'
    match = re.match(reg_exp, url)
    return match.group(2) if match and len(match.group(2)) == 11 else None

@app.route('/api/info', methods=['GET'])
def get_video_info():
    """유튜브 링크를 기반으로 비디오의 상세 메타데이터 및 화질별 다운로드 스트림 주소를 추출합니다."""
    video_url = request.args.get('url')
    if not video_url:
        return jsonify({"success": False, "error": "유튜브 주소(url)가 제공되지 않았습니다."}), 400

    video_id = extract_video_id(video_url)
    if not video_id:
        return jsonify({"success": False, "error": "올바른 유튜브 주소 형식이 아닙니다."}), 400

    target_url = f"https://www.youtube.com/watch?v={video_id}"

    # yt-dlp 최적화 설정 (비디오 실제 다운로드 없이 메타데이터 정보만 신속 수집)
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'format': 'best'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 유튜브 페이지 분석 실행
            info = ydl.extract_info(target_url, download=False)
            
            title = info.get('title', 'YouTube Video')
            thumbnail = info.get('thumbnail', f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg")
            
            formats = info.get('formats', [])
            video_tracks = []
            audio_tracks = []

            # 획득한 포맷 스트림을 순회하며 호환성 높은 규격으로 재구성
            for f in formats:
                # 1. 비디오 트랙 수집 (비디오 코덱이 명시되어 있고 대중적인 표준 해상도 매칭)
                if f.get('vcodec') != 'none':
                    res_height = f.get('height')
                    if res_height in [360, 720, 1080]:
                        video_tracks.append({
                            "format_id": f.get('format_id'),
                            "resolution": f"{res_height}p",
                            "ext": f.get('ext', 'mp4'),
                            "filesize": f.get('filesize') or f.get('filesize_approx') or 0,
                            # 현재 구동 중인 백엔드 호스트를 기준으로 인코딩 브릿지 URL 바인딩
                            "download_url": f"/api/download?url={video_url}&format_id={f.get('format_id')}&ext={f.get('ext')}"
                        })

                # 2. 오디오 트랙 수집 (오디오 전용 코덱인 경우만 발췌)
                elif f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    audio_tracks.append({
                        "format_id": f.get('format_id'),
                        "ext": f.get('ext', 'm4a'),
                        "bitrate": f"{int(f.get('abr', 128))}kbps",
                        "filesize": f.get('filesize') or f.get('filesize_approx') or 0,
                        "download_url": f"/api/download?url={video_url}&format_id={f.get('format_id')}&ext={f.get('ext')}"
                    })

            # 해상도가 높은 순서대로 리스트 재정렬
            video_tracks = sorted(video_tracks, key=lambda x: int(x['resolution'].replace('p','')), reverse=True)
            
            return jsonify({
                "success": True,
                "title": title,
                "thumbnail": thumbnail,
                "video_tracks": video_tracks,
                "audio_tracks": audio_tracks[:4] # 과도한 리스트 바인딩 제한
            })

    except Exception as e:
        return jsonify({"success": False, "error": f"유튜브 데이터 분석 중 오류가 발생했습니다: {str(e)}"}), 500


@app.route('/api/download', methods=['GET'])
def download_stream():
    """
    구글 유튜브 미디어 서버로부터 바이너리를 실시간 청크 단위로 읽어와
    CORS 정책을 제로화한 구조로 프론트엔드로 버퍼 중개(Proxying) 처리를 진행합니다.
    """
    video_url = request.args.get('url')
    format_id = request.args.get('format_id')
    ext = request.args.get('ext', 'mp4')

    if not video_url or not format_id:
        return "필수 파라미터가 누락되었습니다.", 400

    ydl_opts = {
        'format': format_id, 
        'quiet': True,
        'no_warnings': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            stream_url = info.get('url')
            title = info.get('title', 'YouTube_Download')
            
            if not stream_url:
                return "다이렉트 스트림 경로를 획득하지 못했습니다.", 404

            # 구글 비디오 CDN 서버에 요청할 브라우저 에뮬레이터 헤더
            req_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            # 🚀 [메모리 과부하 극복 핵심]: stream=True 옵션을 적용하여 파일을 메모리에 올리지 않고 파이핑 연결
            req = requests.get(stream_url, headers=req_headers, stream=True)
            
            # 실시간 청크 전송 발생기 (Generator)
            def generate_chunks():
                for chunk in req.iter_content(chunk_size=1024 * 64): # 64KB 단위 슬라이싱 전송
                    if chunk:
                        yield chunk

            # 운영체제별 파일 시스템 충돌을 막기 위해 파일명 정제
            safe_filename = "".join([c for c in title if c.isalnum() or c in [' ', '_', '-']]).strip()
            
            # 스트리밍 응답 캡슐화 구성 및 CORS 정책 오픈
            response = Response(stream_with_context(generate_chunks()), content_type=req.headers.get('Content-Type'))
            response.headers['Content-Disposition'] = f'attachment; filename="{safe_filename}.{ext}"'
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            return response

    except Exception as e:
        return f"미디어 스트림 프록시 통신 실패: {str(e)}", 500


if __name__ == '__main__':
    # 🚀 Render 배포용 핵심 환경 변수(PORT) 바인딩 루틴
    # 포트가 지정되지 않았을 경우 로컬 환경에 맞춘 5000번 기본값 유지
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
