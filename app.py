import asyncio
import aiofiles
import aiohttp
import os
import uuid
import re
import logging
import threading
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, session
from flask_session import Session
from googleapiclient.discovery import build

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

app.config['SESSION_TYPE'] = 'filesystem'  # Use 'redis' for scalability
app.config['SESSION_FILE_DIR'] = './flask_session/'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'session:'

Session(app)

export_states = {}
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

def setup_logging():
    logging.basicConfig(filename='app.log', level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

setup_logging()

def get_video_info(video_id):
    try:
        request = youtube.videos().list(part='snippet', id=video_id)
        response = request.execute()
        if response['items']:
            return response['items'][0]['snippet']['title']
        else:
            logging.warning(f"Video with ID {video_id} not found or deleted")
            return "Deleted video"
    except Exception as e:
        logging.error(f"Unexpected error occurred while fetching video info: {e}")
        return "API Error"

def get_playlist_id(entry):
    playlist_id = ""
    if "&si=" in entry:
        entry = entry.split("&si=")[0]
    if len(entry) == 34:
        playlist_id = entry
    elif "youtube.com/playlist?list=" in entry:
        parsed_url = urlparse(entry)
        query_params = parse_qs(parsed_url.query)
        playlist_id = query_params.get("list", [""])[0]
    elif "st=" in entry:
        playlist_id = entry.split("st=")[1]
    return playlist_id

def get_video_name(url):
    if 'youtube.com' in url or 'youtu.be' in url:
        try:
            video_id_match = re.search(r'v=([^&]+)', url)
            if not video_id_match:
                # Handle shortened URLs (e.g., youtu.be/VIDEO_ID)
                video_id_match = re.search(r'youtu.be/([^?&]+)', url)
            
            if video_id_match:
                video_id = video_id_match.group(1)
                return get_video_info(video_id)
            else:
                logging.error(f"Failed to extract video ID from URL: {url}")
                return "Invalid URL"
        except Exception as e:
            logging.error(f"Error retrieving video name: {e}")
            return "API Error"
    else:
        return "HTTP Video"

async def get_video_thumbnail(session: aiohttp.ClientSession, url: str) -> str:
    video_id = re.search(r'v=([^&]+)', url).group(1)
    video_info = get_video_info(video_id)
    if video_info:
        return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    return ""

async def download_thumbnail(url: str, folder_path: str, session: aiohttp.ClientSession):
    video_id = re.search(r'v=([^&]+)', url).group(1)
    thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    if thumbnail_url:
        async with aiofiles.open(os.path.join(folder_path, f"{video_id}.jpg"), mode='wb') as file:
            async with session.get(thumbnail_url) as response:
                content = await response.read()
                await file.write(content)

async def async_export(file_path, url_list, name_list, download_thumbnails, url_prefix, progress_id):
    logging.debug("Starting export to file")
    try:
        async with aiofiles.open(file_path, mode='w', encoding='utf-8') as file:
            #async with aiohttp.ClientSession() as session:
                #if download_thumbnails:
                    #tasks = [download_thumbnail(url, 'static/thumbnails', session) for url in url_list if "youtube.com" in url]
                    #await asyncio.gather(*tasks)
                    #logging.debug(f"Thumbnail download progress: {export_states[progress_id]['export_progress']}%")
                
            for i, url in enumerate(url_list):
                prefixed_url = f"{url_prefix}{url}" if url_prefix else url
                await file.write(f"@{prefixed_url}\n~{name_list[i]}\n\n")
                export_states[progress_id]['export_progress'] = 100 * (i + 1) / len(url_list)
                logging.debug(f"Export progress: {export_states[progress_id]['export_progress']}%")
                await asyncio.sleep(0)
        
        logging.debug(f"Export complete. File saved to {file_path}")
        export_states[progress_id]['export_progress'] = 100
        export_states[progress_id]['exporting'] = False
        export_states[progress_id]['status'] = 'success'
    except Exception as e:
        logging.error(f"Export failed: {e}")
        export_states[progress_id]['status'] = 'error'
        export_states[progress_id]['error_message'] = str(e)
        export_states[progress_id]['export_progress'] = 0
    finally:
        export_states[progress_id]['exporting'] = False
    return file_path

@app.route('/export', methods=['POST'])
def export_data():
    progress_id = str(uuid.uuid4())  # Generate a unique ID for tracking progress
    data = request.form
    file_name = progress_id
    folder_path = data.get("folder_path", ".")
    download_thumbnails = data.get("download_thumbnails") == 'on'
    url_prefix = data.get("prefix", "")

    if 'url_list' not in session or 'name_list' not in session:
        return jsonify({"message": "Error: session lists are not set"}), 400

    url_list = session['url_list']
    name_list = session['name_list']

    export_states[progress_id] = {
        'exporting': True,
        'export_progress': 0,
        'file_path': os.path.join(folder_path, f"{file_name}.txt"),
        'download_thumbnails': download_thumbnails,
        'url_prefix': url_prefix
    }

    if len(url_list) != len(name_list):
        export_states[progress_id]['exporting'] = False
        return jsonify({"message": "Error: lists are not the same length"}), 400

    def run_export():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            file_path = loop.run_until_complete(async_export(
                export_states[progress_id]['file_path'],
                url_list,
                name_list,
                export_states[progress_id]['download_thumbnails'],
                export_states[progress_id]['url_prefix'],
                progress_id
            ))
            export_states[progress_id]['exporting'] = False
            export_states[progress_id]['file_path'] = file_path
        except Exception as e:
            logging.error(f"Export failed: {e}")
            export_states[progress_id]['status'] = 'error'
            export_states[progress_id]['error_message'] = str(e)
        finally:
            export_states[progress_id]['exporting'] = False

    threading.Thread(target=run_export).start()

    return redirect(url_for('results', progress_id=progress_id))

@app.route('/results', methods=['GET'])
def results():
    progress_id = request.args.get('progress_id')
    return render_template('results.html', progress_id=progress_id)

@app.route('/progress/<progress_id>', methods=['GET'])
def check_progress(progress_id):
    state = export_states.get(progress_id)

    if state is None:
        return jsonify({"progress": 0, "status": "error", "error_message": "Invalid or Expired ID"})

    progress = state.get('export_progress', 0)
    status = state.get('status', 'in-progress')
    error_message = state.get('error_message', '')

    if state.get('exporting', False):
        return jsonify({"progress": progress, "status": status, "error_message": error_message})
    else:
        return jsonify({"progress": 100, "status": status, "error_message": error_message})

def is_valid_youtube_url(url):
    return 'youtube.com/watch' in url

def is_valid_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ['http', 'https']

@app.route('/')
def index():
    if 'url_list' not in session:
        session['url_list'] = []
    if 'name_list' not in session:
        session['name_list'] = []
    if 'thumbnail_list' not in session:
        session['thumbnail_list'] = []
    return render_template('index.html')

@app.route('/download_thumbnail', methods=['POST'])
def download_thumbnail_route():
    url = request.form.get('url')
    if is_valid_youtube_url(url):
        #video_id = re.search(r'v=([^&]+)', url).group(1)
        download_path = 'static/thumbnails'
        os.makedirs(download_path, exist_ok=True)
        async def run_download():
            async with aiohttp.ClientSession() as session:
                await download_thumbnail(url, download_path, session)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_download())
        flash('Thumbnail download completed', 'success')
    else:
        flash('Invalid YouTube URL', 'error')
    return redirect(url_for('index'))

@app.route('/clear_links', methods=['POST'])
def clear_links():
    session.pop('url_list', None)
    session.pop('name_list', None)
    session.pop('thumbnail_list', None)
    return jsonify({"message": "Links cleared successfully"})

@app.route('/load_playlist', methods=['POST'])
def load_playlist():
    playlist_url = request.form.get("playlist_url")
    playlist_id = get_playlist_id(playlist_url)

    if not playlist_id:
        flash("Invalid playlist URL")
        return jsonify({"message": "Invalid playlist URL"}), 400

    try:
        url_list = session.get('url_list', [])
        name_list = session.get('name_list', [])
        #thumbnail_list = session.get('thumbnail_list', [])
        initial_count = len(url_list)
            
        playlist_request = youtube.playlistItems().list(
            part='snippet',
            playlistId=playlist_id,
            maxResults=100
        )

        while playlist_request is not None:
            response = playlist_request.execute()
            for item in response['items']:
                video_id = item['snippet']['resourceId']['videoId']
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                video_title = item['snippet']['title']
                #thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

                url_list.append(video_url)
                name_list.append(video_title)
                #thumbnail_list.append(thumbnail_url)

            playlist_request = youtube.playlistItems().list(
                part='snippet',
                playlistId=playlist_id,
                maxResults=100,
                pageToken=response.get('nextPageToken')
            ) if response.get('nextPageToken') else None

        session['url_list'] = url_list
        session['name_list'] = name_list
        #session['thumbnail_list'] = thumbnail_list
        new_count = len(url_list) - initial_count

        return jsonify({"message": f"Loaded {new_count} videos from playlist", "success": True})
    except Exception as e:
        logging.error(f"Error loading playlist: {e}")
        return jsonify({"message": "Error loading playlist", "success": False}), 500

@app.route('/load_urls', methods=['POST'])
def load_urls():
    urls = request.form.get("urls").strip().split('\n')
    initial_count = len(session.get('url_list', []))

    async def process_url(url, aiohttp_session):
        if url.strip():
            url_list = session.get('url_list', [])
            name_list = session.get('name_list', [])
            #thumbnail_list = session.get('thumbnail_list', [])

            url_list.append(url)
            name_list.append(get_video_name(url))
            #thumbnail_list.append(await get_video_thumbnail(aiohttp_session, url))

            session['url_list'] = url_list
            session['name_list'] = name_list
            #session['thumbnail_list'] = thumbnail_list

    async def run_process():
        async with aiohttp.ClientSession() as aiohttp_session:
            await asyncio.gather(*(process_url(url, aiohttp_session) for url in urls))

    asyncio.run(run_process())
    new_count = len(session.get('url_list', [])) - initial_count

    return jsonify({"message": f"Loaded {new_count} URLs", "success": True})

@app.route('/url_count', methods=['GET'])
def get_url_count():
    return jsonify({'count': len(session.get('url_list', []))})

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    file_path = os.path.join('.', filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return jsonify({"message": "File not found"})

@app.route('/check_updates', methods=['GET'])
def check_updates():
    latest_version = '1.0.0'
    current_version = '1.0.0'
    if current_version != latest_version:
        return jsonify({"update_available": True, "latest_version": latest_version})
    return jsonify({"update_available": False})

if __name__ == '__main__':
    app.run(debug=False)
