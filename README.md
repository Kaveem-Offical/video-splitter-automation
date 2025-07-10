# My Flask Video Processing Server

This project is a Flask application designed for processing videos. It allows users to download videos from a URL, split them into segments, process each segment with overlays, and upload the final videos to cloud storage.

## Project Structure

```
my-flask-server
├── app.py                # Main server file for the Flask application
├── requirements.txt      # Lists the dependencies required for the project
├── Dockerfile            # Instructions to build a Docker image for the application
├── render.yaml           # Deployment configuration file
├── Poppins-Regular.ttf    # Font file used for overlay text in videos
├── image.png             # Top overlay image used in video segments
├── end_credit.mp4        # End credit video file used in the final segment
└── README.md             # Documentation for the project
```

## Setup Instructions

1. **Clone the repository:**
   ```
   git clone <repository-url>
   cd my-flask-server
   ```

2. **Install dependencies:**
   You can install the required packages using pip:
   ```
   pip install -r requirements.txt
   ```

3. **Run the application:**
   You can start the Flask application by running:
   ```
   python app.py
   ```

4. **Docker Setup:**
   To build and run the application using Docker, use the following commands:
   ```
   docker build -t my-flask-server .
   docker run -p 5000:5000 my-flask-server
   ```

## Usage

- **Health Check Endpoint:**
  You can check the health of the service by accessing:
  ```
  GET /health
  ```

- **Process Video Endpoint:**
  To process a video, send a POST request to:
  ```
  POST /process
  ```
  with a JSON body containing `video_url` and `movie_name`.

## Notes

- Ensure that you have the necessary environment variables set for Firebase and any other services you plan to use.
- The font file `Poppins-Regular.ttf`, the image `image.png`, and the video `end_credit.mp4` should be present in the project directory for the video processing to work correctly.

## License

This project is licensed under the MIT License. See the LICENSE file for details.