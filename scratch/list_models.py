import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

_SA_KEY_PATH = os.path.join(os.path.dirname(__file__), 'service-account.json')
if os.path.exists(_SA_KEY_PATH):
    os.environ.setdefault('GOOGLE_APPLICATION_CREDENTIALS', _SA_KEY_PATH)

_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "video-gen-492111")
_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

try:
    client = genai.Client(vertexai=True, project=_PROJECT_ID, location=_LOCATION)
    print(f"Listing models for project {_PROJECT_ID} in {_LOCATION}...")
    for model in client.models.list():
        print(f" - {model.name}")
except Exception as e:
    print(f"Error: {e}")
