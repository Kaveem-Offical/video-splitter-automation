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
import cloudinary
import cloudinary.uploader
import cloudinary.api
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration class
class Config:
    FONT_FILE = "Poppins-Regular.ttf"
    TOP_IMAGE = "image.png"
    END_CREDIT = "end_credit.png"

# Configure logging with more detailed format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log', mode='w')
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Store active temp directories for cleanup
active_temp_dirs = set()

def cleanup_all_temp_dirs():
    global active_temp_dirs
    for temp_dir in list(active_temp_dirs):
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.info(f"✅ Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Error cleaning up temp directory {temp_dir}: {str(e)}")
    active_temp_dirs.clear()

def signal_handler(signum, frame):
    logger.info(f"🔄 Received signal {signum}, cleaning up...")
    cleanup_all_temp_dirs()
    exit(0)

atexit.register(cleanup_all_temp_dirs)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

def add_temp_dir(temp_dir):
    global active_temp_dirs
    active_temp_dirs.add(temp_dir)
    logger.info(f"📁 Added temp directory: {temp_dir}")

def remove_temp_dir(temp_dir):
    global active_temp_dirs
    active_temp_dirs.discard(temp_dir)
    logger.info(f"🗑️ Removed temp directory from tracking: {temp_dir}")

def check_ffmpeg():
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10)
        is_available = result.returncode == 0
        logger.info(f"🎬 FFmpeg availability: {'✅ Available' if is_available else '❌ Not available'}")
        return is_available
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error(f"❌ FFmpeg check failed: {str(e)}")
        return False

def process_segment(segment_path, idx, movie_name, output_dir):
    output_file = os.path.join(output_dir, f"{movie_name}_part_{idx:03d}.mp4")

    ffmpeg_command = [
        "ffmpeg",
        "-i", segment_path,
        "-loop", "1", "-t", "3", "-i", Config.END_CREDIT,
        "-i", Config.TOP_IMAGE,
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
        """.replace("\n", ""),
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

    try:
        subprocess.run(ffmpeg_command, check=True)
        logger.info(f"✅ Processed segment saved: {output_file}")
        return output_file
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Error: {e}")
        raise

# The rest of your existing application code should follow here...

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
