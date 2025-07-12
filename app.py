import os
import tempfile
import requests
import logging
import subprocess
import shutil
import signal
import atexit
import gc
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

active_temp_dirs = set()

def cleanup_all_temp_dirs():
    for temp_dir in list(active_temp_dirs):
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Error cleaning up {temp_dir}: {str(e)}")
    active_temp_dirs.clear()

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, cleaning up...")
    cleanup_all_temp_dirs()
    exit(0)

atexit.register(cleanup_all_temp_dirs)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def add_temp_dir(temp_dir):
    active_temp_dirs.add(temp_dir)

def remove_temp_dir(temp_dir):
    active_temp_dirs.discard(temp_dir)

def download_video(url, temp_dir):
    logger.info(f"Downloading video from: {url}")
    response = requests.get(url, stream=True)
    response.raise_for_status()
    ext = ".mp4"
    video_path = os.path.join(temp_dir, f"input_video{ext}")
    with open(video_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    logger.info(f"Downloaded video to: {video_path}")
    return video_path

def split_video(video_path, segment_duration):
    temp_dir = os.path.dirname(video_path)
    segment_pattern = os.path.join(temp_dir, "segment_%03d.mp4")
    cmd = [
        "ffmpeg", "-i", video_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_duration),
        "-reset_timestamps", "1",
        segment_pattern
    ]
    subprocess.run(cmd, check=True)
    return sorted([os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.startswith("segment_")])

def process_segment_with_overlay(segment_path, idx, movie_name, output_dir):
    output_file = os.path.join(output_dir, f"{movie_name}_part_{idx:03d}.mp4")
    cmd = [
    "ffmpeg",
    "-i", segment_path,                             # Main segment
    "-loop", "1", "-t", "3", "-i", "end_credit.png",# End credit (3s image as video)
    "-i", "image.png",                              # Top banner image
    "-filter_complex",
    f"""
    [2:v]scale=1080:-1,setsar=1[top];
    [0:v]scale=1080:1312:force_original_aspect_ratio=decrease,pad=1080:1920:0:608:color=black,setsar=1[main];
    [main][top]overlay=0:0[over];
    [over]drawtext=text='Part No - {idx}':fontfile={Config.FONT_FILE}:fontsize=48:fontcolor=white:x=(w-tw)/2:y=1220[txt1];
    [txt1]drawtext=text='{movie_name}':fontfile={Config.FONT_FILE}:fontsize=48:fontcolor=white:x=(w-tw)/2:y=1266[txt2];
    [1:v]scale=1080:1920,setsar=1[end];
    [txt2][end]concat=n=2:v=1:a=0[outv];
    [0:a]aresample=async=1[outa]
    """.replace("\n", ""),  # FFmpeg doesn't like actual line breaks
    "-map", "[outv]",
    "-map", "[outa]",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "23",
    "-c:a", "aac",
    "-b:a", "128k",
    "-y",
    output_file
]

    subprocess.run(cmd, check=True)
    return output_file

@app.route('/split-video', methods=['POST'])
def split_video_endpoint():
    request_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info(f"[REQUEST-{request_id}] Split video request received")
    temp_dir = tempfile.mkdtemp(prefix=f"video_split_{request_id}_")
    add_temp_dir(temp_dir)

    try:
        data = request.get_json()
        video_url = data.get("video_url")
        movie_name = data.get("movie_name", "video")
        segment_duration = int(data.get("segment_duration", 60))

        if not video_url:
            return jsonify({"success": False, "message": "Missing video_url"}), 400

        video_path = download_video(video_url, temp_dir)
        segments = split_video(video_path, segment_duration)
        output_dir = os.path.join(temp_dir, "processed")
        os.makedirs(output_dir, exist_ok=True)

        processed_files = []
        for idx, segment in enumerate(segments, 1):
            out = process_segment_with_overlay(segment, idx, movie_name, output_dir)
            processed_files.append(out)

        cleanup_all_temp_dirs()

        return jsonify({
            "success": True,
            "message": f"Processed {len(processed_files)} segments",
            "files": processed_files
        })

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        cleanup_all_temp_dirs()
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
