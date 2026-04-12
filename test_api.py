import youtube_api
import requests
config = youtube_api.load_config()
api_key = config.get('api_key')
print("API_KEY:", repr(api_key))

params = {
    'part': 'snippet',
    'type': 'channel',
    'q': 'BBC',
    'maxResults': 5,
    'key': api_key
}
resp = requests.get("https://www.googleapis.com/youtube/v3/search", params=params).json()
print("Response keys:", resp.keys())
if 'error' in resp:
    print("Error:", resp['error'])
elif 'items' in resp:
    print("Found items:", len(resp['items']))
