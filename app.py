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

# Configuration for image, text overlay, and end credit
class Config:
    TOP_IMAGE = "image.png"  # 1920x1080 image for overlay
    FONT_FILE = "Poppins-Regular.ttf"  # Font file for text overlay
    END_CREDIT = "end_credit.mp4"  # End credit video (portrait, 1080x1920)

# Store active temp directories for cleanup
active_temp_dirs = set()

def cleanup_all_temp_dirs():
    """Clean up all active temporary directories"""
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
    """Handle shutdown signals"""
    logger.info(f"🔄 Received signal {signum}, cleaning up...")
    cleanup_all_temp_dirs()
    exit(0)

# Register cleanup handlers
atexit.register(cleanup_all_temp_dirs)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

def add_temp_dir(temp_dir):
    """Add temp directory to tracking"""
    global active_temp_dirs
    active_temp_dirs.add(temp_dir)
    logger.info(f"📁 Added temp directory: {temp_dir}")

def remove_temp_dir(temp_dir):
    """Remove temp directory from tracking"""
    global active_temp_dirs
    active_temp_dirs.discard(temp_dir)
    logger.info(f"🗑️ Removed temp directory from tracking: {temp_dir}")

def check_ffmpeg():
    """Check if FFmpeg is installed"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        is_available = result.returncode == 0
        logger.info(f"🎬 FFmpeg availability: {'✅ Available' if is_available else '❌ Not available'}")
        return is_available
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error(f"❌ FFmpeg check failed: {str(e)}")
        return False

def check_dependencies():
    """Check if required files exist"""
    missing_items = []
    for file_path in [Config.TOP_IMAGE, Config.FONT_FILE, Config.END_CREDIT]:
        if not os.path.exists(file_path):
            missing_items.append(f"Required file: {file_path}")
    if missing_items:
        logger.error("Missing dependencies:")
        for item in missing_items:
            logger.error(f"  - {item}")
        return False
    logger.info("All dependencies found")
    return True

def get_video_duration(video_path):
    """Get video duration using FFmpeg"""
    try:
        logger.info(f"⏱️ Getting duration for: {os.path.basename(video_path)}")
        cmd = [
            'ffprobe', '-v', 'quiet', '-show_entries', 
            'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            logger.info(f"📊 Video duration: {duration:.2f} seconds")
            return duration
        else:
            logger.error(f"❌ FFprobe error: {result.stderr}")
            return 0
    except Exception as e:
        logger.error(f"❌ Error getting video duration: {str(e)}")
        return 0

def get_video_info(video_path):
    """Get detailed video information"""
    try:
        logger.info(f"📊 Analyzing video: {os.path.basename(video_path)}")
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            import json
            info = json.loads(result.stdout)
            
            video_stream = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
            format_info = info['format']
            
            video_info = {
                'duration': float(format_info.get('duration', 0)),
                'size': int(format_info.get('size', 0)),
                'format': format_info.get('format_name', 'unknown'),
                'width': int(video_stream.get('width', 0)) if video_stream else 0,
                'height': int(video_stream.get('height', 0)) if video_stream else 0,
                'fps': eval(video_stream.get('r_frame_rate', '0/1')) if video_stream else 0
            }
            
            logger.info(f"📈 Video info - Duration: {video_info['duration']:.2f}s, "
                       f"Size: {video_info['size']/1024/1024:.2f}MB, "
                       f"Resolution: {video_info['width']}x{video_info['height']}")
            return video_info
        else:
            logger.error(f"❌ FFprobe error: {result.stderr}")
            return None
    except Exception as e:
        logger.error(f"❌ Error getting video info: {str(e)}")
        return None

def split_video_ffmpeg(video_path, segment_duration=60, overlap=0):
    """Split video using FFmpeg with image overlay, text, and end credit"""
    try:
        logger.info(f"🔪 Starting video split - Duration: {segment_duration}s, Overlap: {overlap}s")
        
        # Check dependencies
        if not check_dependencies():
            raise Exception("Missing required dependencies (image, font, or end credit file)")
        
        # Get video duration
        duration = get_video_duration(video_path)
        if duration == 0:
            raise Exception("Could not determine video duration")
        
        # Calculate number of segments
        effective_duration = segment_duration - overlap
        num_segments = int(duration // effective_duration) + (1 if duration % effective_duration > 0 else 0)
        
        logger.info(f"📋 Will create {num_segments} segments from {duration:.2f}s video")
        
        segment_files = []
        segment_info = []
        temp_dir = os.path.dirname(video_path)
        
        # Get video name for text overlay
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        video_name_escaped = video_name.replace("'", "\\'").replace(":", "\\:")
        
        for i in range(num_segments):
            start_time = i * effective_duration
            actual_duration = min(segment_duration, duration - start_time)
            
            if actual_duration <= 0:
                break
                
            segment_filename = f"segment_{i+1:03d}.mp4"
            segment_path = os.path.join(temp_dir, segment_filename)
            
            logger.info(f"⚡ Creating segment {i+1}/{num_segments}: {segment_filename}")
            
            # FFmpeg command to extract segment, add overlay/text, and concatenate end credit
            cmd = [
                'ffmpeg', '-i', video_path, '-i', Config.TOP_IMAGE, '-i', Config.END_CREDIT,
                '-filter_complex',
                f"[0:v]trim=start={start_time}:duration={actual_duration},scale=1080:1312:force_original_aspect_ratio=decrease,pad=1080:1312:0:0:color=black,setsar=1[main_vid];"
                f"[1:v]scale=1080:-1,setsar=1[top_image];"
                f"[main_vid]pad=1080:1920:0:608:color=black,setsar=1[padded_base];"
                f"[padded_base][top_image]overlay=0:0[combined];"
                f"[combined]drawtext=text='Part No - {i+1}':fontfile={Config.FONT_FILE}:fontsize=48:fontcolor=white:x=(w-tw)/2:y=1220[with_part];"
                f"[with_part]drawtext=text='{video_name_escaped}':fontfile={Config.FONT_FILE}:fontsize=48:fontcolor=white:x=(w-tw)/2:y=1266[with_text];"
                f"[2:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[end_scaled];"
                f"[with_text][end_scaled]concat=n=2:v=1:a=0[outv];"
                f"[0:a]atrim=start={start_time}:duration={actual_duration},asetpts=PTS-STARTPTS,aresample=async=1[outa]",
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart', '-y',
                segment_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                segment_files.append(segment_path)
                segment_info.append({
                    'filename': segment_filename,
                    'start_time': start_time,
                    'duration': actual_duration,
                    'segment_number': i + 1,
                    'file_size': os.path.getsize(segment_path)
                })
                logger.info(f"✅ Segment {i+1} created successfully - Size: {os.path.getsize(segment_path)/1024/1024:.2f}MB")
            else:
                logger.error(f"❌ FFmpeg error for segment {i+1}: {result.stderr}")
                # Continue with other segments even if one fails
        
        logger.info(f"🎉 Video split completed: {len(segment_files)} segments created")
        return segment_files, segment_info
        
    except Exception as e:
        logger.error(f"❌ Error splitting video with FFmpeg: {str(e)}")
        raise

def verify_cloudinary_config():
    """Verify Cloudinary configuration"""
    try:
        logger.info("🔐 Verifying Cloudinary configuration...")
        result = cloudinary.api.ping()
        is_authenticated = result.get('status') == 'ok'
        logger.info(f"☁️ Cloudinary status: {'✅ Authenticated' if is_authenticated else '❌ Failed'}")
        return is_authenticated
    except Exception as e:
        logger.error(f"❌ Cloudinary authentication failed: {str(e)}")
        return False

def download_video(url, temp_dir):
    """Download video from URL to temporary directory"""
    try:
        logger.info(f"⬇️ Starting video download from: {url}")
        
        head_response = requests.head(url, timeout=30)
        file_size = int(head_response.headers.get('content-length', 0))
        logger.info(f"📦 File size: {file_size/1024/1024:.2f}MB" if file_size > 0 else "📦 File size: Unknown")
        
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '')
        if 'video/mp4' in content_type or url.endswith('.mp4'):
            ext = '.mp4'
        elif 'video/avi' in content_type or url.endswith('.avi'):
            ext = '.avi'
        elif 'video/mov' in content_type or url.endswith('.mov'):
            ext = '.mov'
        else:
            ext = '.mp4'
        
        video_path = os.path.join(temp_dir, f"input_video{ext}")
        
        downloaded = 0
        last_progress = 0
        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if file_size > 0:
                    progress = (downloaded / file_size) * 100
                    if progress - last_progress >= 10:
                        logger.info(f"📈 Download progress: {progress:.1f}%")
                        last_progress = progress
        
        logger.info(f"✅ Video downloaded successfully: {os.path.basename(video_path)} ({downloaded/1024/1024:.2f}MB)")
        return video_path
    except Exception as e:
        logger.error(f"❌ Error downloading video: {str(e)}")
        raise

def upload_to_cloudinary(file_path, folder_name="video_splits", segment_info=None):
    """Upload file to Cloudinary with metadata"""
    try:
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        logger.info(f"☁️ Uploading to Cloudinary: {filename} ({file_size/1024/1024:.2f}MB)")
        
        context = {}
        if segment_info:
            context.update({
                'segment_number': segment_info.get('segment_number', 0),
                'start_time': segment_info.get('start_time', 0),
                'duration': segment_info.get('duration', 0),
                'file_size': segment_info.get('file_size', 0)
            })
        
        result = cloudinary.uploader.upload(
            file_path,
            resource_type="video",
            folder=folder_name,
            public_id=f"{folder_name}/{filename.replace('.mp4', '')}",
            overwrite=True,
            context=context,
            timeout=300,
            eager=[
                {"quality": "auto", "format": "mp4"},
                {"quality": "auto:low", "format": "mp4", "width": 640}
            ],
            eager_async=True
        )
        
        logger.info(f"✅ Successfully uploaded {filename} to Cloudinary - Public ID: {result['public_id']}")
        return {
            'public_id': result['public_id'],
            'secure_url': result['secure_url'],
            'url': result['url'],
            'format': result['format'],
            'resource_type': result['resource_type'],
            'bytes': result['bytes'],
            'duration': result.get('duration', 0),
            'width': result.get('width', 0),
            'height': result.get('height', 0),
            'segment_info': segment_info
        }
        
    except Exception as e:
        logger.error(f"❌ Error uploading {filename} to Cloudinary: {str(e)}")
        return None

def cleanup_temp_files(temp_dir):
    """Clean up temporary files"""
    try:
        if os.path.exists(temp_dir):
            total_size = sum(os.path.getsize(os.path.join(dirpath, filename))
                           for dirpath, dirnames, filenames in os.walk(temp_dir)
                           for filename in filenames)
            
            shutil.rmtree(temp_dir)
            remove_temp_dir(temp_dir)
            logger.info(f"🧹 Cleaned up temp directory: {temp_dir} (freed {total_size/1024/1024:.2f}MB)")
            
            gc.collect()
        else:
            logger.info(f"🧹 Temp directory already cleaned: {temp_dir}")
    except Exception as e:
        logger.warning(f"⚠️ Error cleaning up temp files: {str(e)}")

@app.route('/split-video', methods=['POST'])
def split_video_endpoint():
    """API endpoint to split video and upload to Cloudinary"""
    request_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info(f"🚀 [REQUEST-{request_id}] Starting video split request")
    
    temp_dir = None
    try:
        if not check_ffmpeg():
            logger.error(f"❌ [REQUEST-{request_id}] FFmpeg not available")
            return jsonify({
                'success': False,
                'message': 'FFmpeg is not installed or not in PATH. Please install FFmpeg to use this service.'
            }), 500
        
        data = request.get_json()
        if not data or 'video_url' not in data:
            logger.error(f"❌ [REQUEST-{request_id}] Missing video_url in request")
            return jsonify({
                'success': False,
                'message': 'video_url is required in request body'
            }), 400
        
        video_url = data['video_url']
        segment_duration = data.get('segment_duration', 60)
        overlap = data.get('overlap', 0)
        folder_name = data.get('folder_name', 'video_splits')
        upload_to_cloud = data.get('upload_to_cloud', True)
        
        if segment_duration <= 0:
            logger.error(f"❌ [REQUEST-{request_id}] Invalid segment_duration: {segment_duration}")
            return jsonify({
                'success': False,
                'message': 'segment_duration must be greater than 0'
            }), 400
        
        if overlap < 0 or overlap >= segment_duration:
            logger.error(f"❌ [REQUEST-{request_id}] Invalid overlap: {overlap}")
            return jsonify({
                'success': False,
                'message': 'overlap must be between 0 and segment_duration'
            }), 400
        
        logger.info(f"📋 [REQUEST-{request_id}] Processing config - Duration: {segment_duration}s, Overlap: {overlap}s, Upload: {upload_to_cloud}")
        
        if upload_to_cloud and not verify_cloudinary_config():
            logger.error(f"❌ [REQUEST-{request_id}] Cloudinary configuration failed")
            return jsonify({
                'success': False,
                'message': 'Cloudinary configuration error. Please check your credentials.'
            }), 500
        
        temp_dir = tempfile.mkdtemp(prefix=f"video_split_{request_id}_")
        add_temp_dir(temp_dir)
        logger.info(f"📁 [REQUEST-{request_id}] Created temp directory: {temp_dir}")
        
        start_time = datetime.now()
        
        try:
            logger.info(f"⬇️ [REQUEST-{request_id}] Starting video download")
            video_path = download_video(video_url, temp_dir)
            
            logger.info(f"📊 [REQUEST-{request_id}] Analyzing video")
            video_info = get_video_info(video_path)
            
            logger.info(f"🔪 [REQUEST-{request_id}] Starting video split")
            segment_files, segment_info = split_video_ffmpeg(video_path, segment_duration, overlap)
            
            uploaded_files = []
            failed_uploads = []
            
            if upload_to_cloud:
                logger.info(f"☁️ [REQUEST-{request_id}] Starting uploads to Cloudinary")
                for i, segment_path in enumerate(segment_files):
                    segment_metadata = segment_info[i] if i < len(segment_info) else None
                    upload_result = upload_to_cloudinary(segment_path, folder_name, segment_metadata)
                    if upload_result:
                        uploaded_files.append(upload_result)
                    else:
                        failed_uploads.append(os.path.basename(segment_path))
                
                logger.info(f"📤 [REQUEST-{request_id}] Upload completed - Success: {len(uploaded_files)}, Failed: {len(failed_uploads)}")
            
            cleanup_temp_files(temp_dir)
            temp_dir = None
            
            processing_time = (datetime.now() - start_time).total_seconds()
            
            response = {
                'success': True,
                'request_id': request_id,
                'message': 'Video processing completed successfully',
                'processed_at': datetime.now().isoformat(),
                'processing_time_seconds': round(processing_time, 2),
                'video_info': video_info,
                'processing_config': {
                    'segment_duration': segment_duration,
                    'overlap': overlap,
                    'folder_name': folder_name,
                    'uploaded_to_cloud': upload_to_cloud
                },
                'results': {
                    'segments_created': len(segment_files),
                    'files_uploaded': len(uploaded_files),
                    'failed_uploads': len(failed_uploads)
                }
            }
            
            if upload_to_cloud:
                response['uploaded_files'] = uploaded_files
                if failed_uploads:
                    response['failed_files'] = failed_uploads
                    response['message'] += f" ({len(failed_uploads)} uploads failed)"
            else:
                response['segment_info'] = segment_info
                response['message'] += " (files not uploaded - upload_to_cloud was false)"
            
            logger.info(f"🎉 [REQUEST-{request_id}] Request completed successfully in {processing_time:.2f}s")
            return jsonify(response)
            
        except Exception as e:
            if temp_dir:
                cleanup_temp_files(temp_dir)
            raise e
            
    except Exception as e:
        logger.error(f"❌ [REQUEST-{request_id}] Error processing video: {str(e)}")
        if temp_dir:
            cleanup_temp_files(temp_dir)
        return jsonify({
            'success': False,
            'request_id': request_id,
            'message': f'Error processing video: {str(e)}'
        }), 500

@app.route('/list-videos', methods=['GET'])
def list_videos():
    """List all videos in Cloudinary folder"""
    try:
        folder_name = request.args.get('folder', 'video_splits')
        max_results = int(request.args.get('max_results', 50))
        
        logger.info(f"📋 Listing videos from folder: {folder_name}")
        
        result = cloudinary.api.resources(
            resource_type="video",
            type="upload",
            prefix=folder_name,
            max_results=max_results,
            context=True
        )
        
        videos = []
        for resource in result.get('resources', []):
            video_data = {
                'public_id': resource['public_id'],
                'secure_url': resource['secure_url'],
                'url': resource['url'],
                'format': resource['format'],
                'bytes': resource['bytes'],
                'duration': resource.get('duration', 0),
                'width': resource.get('width', 0),
                'height': resource.get('height', 0),
                'created_at': resource['created_at']
            }
            
            if 'context' in resource:
                video_data['metadata'] = resource['context']
            
            videos.append(video_data)
        
        logger.info(f"📊 Found {len(videos)} videos in folder: {folder_name}")
        return jsonify({
            'success': True,
            'folder': folder_name,
            'total_videos': len(videos),
            'videos': videos
        })
        
    except Exception as e:
        logger.error(f"❌ Error listing videos: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error listing videos: {str(e)}'
        }), 500

@app.route('/video-info', methods=['POST'])
def get_video_info_endpoint():
    """Get information about a video without processing it"""
    request_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info(f"📊 [INFO-{request_id}] Video info request")
    
    temp_dir = None
    try:
        data = request.get_json()
        if not data or 'video_url' not in data:
            return jsonify({
                'success': False,
                'message': 'video_url is required'
            }), 400
        
        video_url = data['video_url']
        
        temp_dir = tempfile.mkdtemp(prefix=f"video_info_{request_id}_")
        add_temp_dir(temp_dir)
        
        try:
            video_path = download_video(video_url, temp_dir)
            
            video_info = get_video_info(video_path)
            
            cleanup_temp_files(temp_dir)
            temp_dir = None
            
            if video_info:
                segment_duration = data.get('segment_duration', 60)
                overlap = data.get('overlap', 0)
                effective_duration = segment_duration - overlap
                estimated_segments = int(video_info['duration'] // effective_duration) + (1 if video_info['duration'] % effective_duration > 0 else 0)
                
                logger.info(f"✅ [INFO-{request_id}] Video info retrieved successfully")
                return jsonify({
                    'success': True,
                    'request_id': request_id,
                    'video_info': video_info,
                    'estimated_segments': estimated_segments,
                    'processing_time_estimate': f"{estimated_segments * 2}-{estimated_segments * 5} seconds"
                })
            else:
                logger.error(f"❌ [INFO-{request_id}] Could not analyze video")
                return jsonify({
                    'success': False,
                    'message': 'Could not analyze video file'
                }), 400
                
        except Exception as e:
            if temp_dir:
                cleanup_temp_files(temp_dir)
            raise e
            
    except Exception as e:
        logger.error(f"❌ [INFO-{request_id}] Error getting video info: {str(e)}")
        if temp_dir:
            cleanup_temp_files(temp_dir)
        return jsonify({
            'success': False,
            'request_id': request_id,
            'message': f'Error getting video info: {str(e)}'
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    logger.info("🔍 Health check requested")
    
    cloudinary_status = verify_cloudinary_config()
    ffmpeg_status = check_ffmpeg()
    
    import psutil
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    health_data = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0',
        'services': {
            'cloudinary_configured': cloudinary_status,
            'ffmpeg_available': ffmpeg_status
        },
        'system': {
            'cpu_percent': cpu_percent,
            'memory_percent': memory.percent,
            'disk_percent': (disk.used / disk.total) * 100,
            'active_temp_dirs': len(active_temp_dirs)
        }
    }
    
    logger.info(f"💚 Health check completed - CPU: {cpu_percent}%, Memory: {memory.percent}%, Disk: {health_data['system']['disk_percent']:.1f}%")
    return jsonify(health_data)

@app.route('/cleanup', methods=['POST'])
def manual_cleanup():
    """Manual cleanup endpoint"""
    try:
        logger.info("🧹 Manual cleanup requested")
        cleanup_all_temp_dirs()
        return jsonify({
            'success': True,
            'message': 'Manual cleanup completed',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"❌ Manual cleanup failed: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Cleanup failed: {str(e)}'
        }), 500

@app.route('/', methods=['GET'])
def index():
    """API documentation"""
    return jsonify({
        'name': 'Video Splitter API',
        'version': '1.0.0',
        'status': 'running',
        'endpoints': {
            'POST /split-video': 'Split video into segments with image overlay, text, and end credit, and optionally upload to Cloudinary',
            'POST /video-info': 'Get video information without processing',
            'GET /list-videos': 'List uploaded videos from Cloudinary',
            'GET /health': 'Health check with system metrics',
            'POST /cleanup': 'Manual cleanup of temporary files'
        },
        'documentation': {
            'split-video': {
                'method': 'POST',
                'body': {
                    'video_url': 'string (required) - URL of video to split',
                    'segment_duration': 'integer (optional) - Duration of each segment in seconds (default: 60)',
                    'overlap': 'integer (optional) - Overlap between segments in seconds (default: 0)',
                    'folder_name': 'string (optional) - Cloudinary folder name (default: video_splits)',
                    'upload_to_cloud': 'boolean (optional) - Whether to upload to Cloudinary (default: true)'
                }
            }
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    logger.info(f"🚀 Starting Video Splitter API on port {port}")
    logger.info(f"🔧 Debug mode: {debug}")
    logger.info(f"📊 System info - Python: {os.sys.version}")
    
    app.run(debug=debug, host='0.0.0.0', port=port)