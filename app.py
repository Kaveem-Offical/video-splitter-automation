import subprocess
import os
import shutil
import requests
import json
import time
import threading
from datetime import datetime, timedelta
from glob import glob
from flask import Flask, request, jsonify
from urllib.parse import urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

app = Flask(__name__)

# Configuration
class Config:
    # Google Drive configuration
    GOOGLE_DRIVE_CREDENTIALS_JSON = os.environ.get('GOOGLE_DRIVE_CREDENTIALS_JSON')
    GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')  # Optional: specific folder ID
    
    # Processing configuration
    SEGMENT_DURATION = 80  # seconds
    OUTPUT_DIR = "output_parts"
    TEMP_DIR = "temp_segments"
    DOWNLOADS_DIR = "downloads"
    FONT_FILE = "Poppins-Regular.ttf"
    TOP_IMAGE = "image.png"
    END_CREDIT = "end_credit.mp4"
    
    # Server configuration
    CLEANUP_INTERVAL = 12 * 60 * 60  # 12 hours in seconds
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB limit

# Initialize Google Drive service
def initialize_google_drive():
    try:
        if Config.GOOGLE_DRIVE_CREDENTIALS_JSON:
            cred_dict = json.loads(Config.GOOGLE_DRIVE_CREDENTIALS_JSON)
            credentials = service_account.Credentials.from_service_account_info(
                cred_dict, scopes=['https://www.googleapis.com/auth/drive.file']
            )
            service = build('drive', 'v3', credentials=credentials)
            return service
        return None
    except Exception as e:
        print(f"Google Drive initialization failed: {e}")
        return None

# Global Google Drive service instance
drive_service = None

# Initialize directories
def setup_directories():
    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    os.makedirs(Config.DOWNLOADS_DIR, exist_ok=True)

# Download video from URL
def download_video(url, filename):
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        # Check file size
        if 'content-length' in response.headers:
            file_size = int(response.headers['content-length'])
            if file_size > Config.MAX_FILE_SIZE:
                raise Exception(f"File too large: {file_size} bytes")
        
        filepath = os.path.join(Config.DOWNLOADS_DIR, filename)
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return filepath
    except Exception as e:
        raise Exception(f"Download failed: {str(e)}")

# Split video into segments
def split_video(input_path, output_dir):
    split_command = [
        "ffmpeg",
        "-i", input_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(Config.SEGMENT_DURATION),
        "-reset_timestamps", "1",
        os.path.join(output_dir, "segment_%03d.mp4")
    ]
    subprocess.run(split_command, check=True)
    return sorted(glob(os.path.join(output_dir, "segment_*.mp4")))

# Process individual segment
def process_segment(segment_path, idx, movie_name, output_dir):
    output_file = os.path.join(output_dir, f"{movie_name}_part_{idx:03d}.mp4")
    
    ffmpeg_command = [
        "ffmpeg",
        "-i", segment_path,
        "-i", Config.TOP_IMAGE,
        "-i", Config.END_CREDIT,
        "-filter_complex",
        f"[1:v]scale=1080:-1,setsar=1[top_image];"
        f"[0:v]scale=1080:1312:force_original_aspect_ratio=decrease,"
        f"pad=1080:1312:0:0:color=black,setsar=1[main_vid];"
        f"[main_vid]pad=1080:1920:0:608:color=black,setsar=1[padded_base];"
        f"[padded_base][top_image]overlay=0:0[combined];"
        f"[combined]drawtext=text='Part No - {idx}':fontfile={Config.FONT_FILE}:"
        f"fontsize=48:fontcolor=white:x=(w-tw)/2:y=1220[with_part];"
        f"[with_part]drawtext=text='{movie_name}':fontfile={Config.FONT_FILE}:"
        f"fontsize=48:fontcolor=white:x=(w-tw)/2:y=1266[with_text];"
        f"[2:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[end_scaled];"
        f"[with_text][end_scaled]concat=n=2:v=1:a=0[outv];"
        f"[0:a]aresample=async=1[outa]",
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
    
    subprocess.run(ffmpeg_command, check=True)
    return output_file

# Create or get Google Drive folder
def create_drive_folder(folder_name, parent_folder_id=None):
    try:
        # Check if folder already exists
        query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
        if parent_folder_id:
            query += f" and parents in '{parent_folder_id}'"
        
        results = drive_service.files().list(q=query).execute()
        items = results.get('files', [])
        
        if items:
            return items[0]['id']
        
        # Create new folder
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        
        if parent_folder_id:
            folder_metadata['parents'] = [parent_folder_id]
        
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
        
    except Exception as e:
        print(f"Error creating/finding folder: {e}")
        return None

# Upload file to Google Drive
def upload_to_google_drive(file_path, destination_name, folder_id=None):
    try:
        if not drive_service:
            raise Exception("Google Drive service not initialized")
        
        # Prepare file metadata
        file_metadata = {
            'name': destination_name
        }
        
        if folder_id:
            file_metadata['parents'] = [folder_id]
        
        # Upload file
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        file_id = file.get('id')
        
        # Make file publicly accessible
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        drive_service.permissions().create(
            fileId=file_id,
            body=permission
        ).execute()
        
        # Return public download URL
        return f"https://drive.google.com/uc?id={file_id}"
        
    except Exception as e:
        raise Exception(f"Google Drive upload failed: {str(e)}")

# Delete files from Google Drive folder
def cleanup_drive_folder(folder_id):
    try:
        if not drive_service or not folder_id:
            return
        
        # Get all files in the folder
        results = drive_service.files().list(
            q=f"parents in '{folder_id}'",
            fields="files(id, name)"
        ).execute()
        
        files = results.get('files', [])
        
        # Delete each file
        for file in files:
            drive_service.files().delete(fileId=file['id']).execute()
            print(f"Deleted: {file['name']}")
            
    except Exception as e:
        print(f"Error cleaning up Drive folder: {e}")

# Main processing function
def process_video(video_url, movie_name):
    try:
        # Create a folder for this movie in Google Drive
        folder_id = create_drive_folder(movie_name, Config.GOOGLE_DRIVE_FOLDER_ID)
        if not folder_id:
            raise Exception("Failed to create Google Drive folder")
        
        # Step 1: Download video
        print(f"Downloading video from: {video_url}")
        parsed_url = urlparse(video_url)
        filename = f"{movie_name}.{parsed_url.path.split('.')[-1]}"
        video_path = download_video(video_url, filename)
        
        # Step 2: Split video into segments
        print("Splitting video into segments...")
        segments = split_video(video_path, Config.TEMP_DIR)
        
        # Step 3: Process each segment
        print("Processing segments...")
        processed_files = []
        upload_urls = []
        
        for idx, segment in enumerate(segments, start=1):
            print(f"Processing part {idx}...")
            output_file = process_segment(segment, idx, movie_name, Config.OUTPUT_DIR)
            processed_files.append(output_file)
            
            # Upload to Google Drive
            destination_name = f"{movie_name}_part_{idx:03d}.mp4"
            url = upload_to_google_drive(output_file, destination_name, folder_id)
            
            upload_urls.append({
                "part": idx,
                "filename": destination_name,
                "url": url
            })
        
        # Clean up temporary files
        print("Cleaning up temporary files...")
        for segment in segments:
            os.remove(segment)
        
        return {
            "success": True,
            "movie_name": movie_name,
            "total_parts": len(upload_urls),
            "drive_folder_id": folder_id,
            "drive_folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
            "files": upload_urls,
            "processed_at": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"Error processing video: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "processed_at": datetime.now().isoformat()
        }

# Clean up old files
def cleanup_files():
    print("Starting cleanup process...")
    try:
        # Remove all directories and their contents
        for directory in [Config.OUTPUT_DIR, Config.TEMP_DIR, Config.DOWNLOADS_DIR]:
            if os.path.exists(directory):
                shutil.rmtree(directory)
        
        # Recreate directories
        setup_directories()
        print("Cleanup completed successfully!")
        
    except Exception as e:
        print(f"Cleanup error: {str(e)}")

# Schedule cleanup every 12 hours
def schedule_cleanup():
    while True:
        time.sleep(Config.CLEANUP_INTERVAL)
        cleanup_files()

# API Routes
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "video-processor"
    })

@app.route('/process', methods=['POST'])
def process_video_endpoint():
    try:
        data = request.get_json()
        
        if not data or 'video_url' not in data or 'movie_name' not in data:
            return jsonify({
                "success": False,
                "error": "Missing required parameters: video_url, movie_name"
            }), 400
        
        video_url = data['video_url']
        movie_name = data['movie_name']
        
        # Process video
        result = process_video(video_url, movie_name)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/cleanup', methods=['POST'])
def manual_cleanup():
    try:
        cleanup_files()
        return jsonify({
            "success": True,
            "message": "Cleanup completed successfully"
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

# Initialize everything
if __name__ == '__main__':
    setup_directories()
    drive_service = initialize_google_drive()
    
    if not drive_service:
        print("Warning: Google Drive service not initialized. Check credentials.")
    
    # Start cleanup scheduler in background
    cleanup_thread = threading.Thread(target=schedule_cleanup, daemon=True)
    cleanup_thread.start()
    
    # Start Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)