import requests
import feedparser
import json
import re
import sys
import time
from datetime import datetime
from dateutil import parser

# --- CONFIGURATION HUB ---
CLOUD_CONFIG = {
    "aws": {
        "rss": "https://aws.amazon.com/about-aws/whats-new/recent/feed/",
        "tf_repo": "hashicorp/terraform-provider-aws",
        "stop_words": {'amazon', 'aws', 'now', 'supports', 'available', 'introducing', 'general', 'availability', 'for', 'with', 'announcing'},
        "service_heuristic": "Amazon (.*?) "
    },
    "azure": {
        "rss": "https://azurecomcdn.azureedge.net/en-us/updates/feed/",
        "tf_repo": "hashicorp/terraform-provider-azurerm",
        "stop_words": {'azure', 'microsoft', 'public', 'preview', 'general', 'availability', 'now', 'available', 'support', 'in'},
        "service_heuristic": "Azure (.*?) "
    },
    "gcp": {
        "rss": "https://cloud.google.com/feeds/gcp-release-notes.xml",
        "tf_repo": "hashicorp/terraform-provider-google",
        "stop_words": {'google', 'cloud', 'platform', 'gcp', 'beta', 'ga', 'release', 'notes', 'available', 'support'},
        "service_heuristic": "^(.*?):" # GCP often starts with "Cloud Spanner: ..."
    }
}

GITHUB_API_BASE = "https://api.github.com/repos"
OUTPUT_FILE = "r2c_lag_data.json"
HEADERS = {
    'User-Agent': 'Rack2Cloud-Bot/2.0',
    'Accept': 'application/vnd.github.v3+json'
}

class FeatureRecord:
    def __init__(self, cloud, service, feature, date, link):
        self.cloud = cloud
        self.service = service
        self.feature = feature
        self.ga_date = date
        self.link = link
        self.tf_status = "Not Supported"
        self.tf_version = "--"
        self.lag_days = 0

    def to_dict(self):
        # Create a unique ID based on the feature name
        clean_name = re.sub(r'[^a-zA-Z0-9]', '-', self.feature[:20]).lower()
        return {
            "id": f"{self.cloud}-{clean_name}",
            "cloud": self.cloud,
            "service": self.service[:20], # Truncate for UI
            "feature": self.feature,
            "link": self.link,
            "status": self.tf_status,
            "version": self.tf_version,
            "lag": self.lag_days,
            "date": self.ga_date.strftime("%Y-%m-%d")
        }

def fetch_feed(cloud_name, config):
    print(f"📡 [{cloud_name.upper()}] Fetching Cloud Feed...")
    try:
        feed = feedparser.parse(config['rss'])
        if not feed.entries:
            print(f"   ⚠️ Warning: {cloud_name} feed is empty.")
            return []
            
        records = []
        # Process last 15 entries per cloud to keep JSON size manageable
        for entry in feed.entries[:15]:
            title = entry.title
            
            # Smart Service Extraction
            service = "General"
            match = re.search(config['service_heuristic'], title)
            if match:
                service = match.group(1).replace(",", "").strip()
            
            # Date Parsing fallback
            try:
                dt = parser.parse(entry.published)
            except:
                dt = datetime.now()

            records.append(FeatureRecord(
                cloud=cloud_name,
                service=service,
                feature=title,
                date=dt,
                link=entry.link
            ))
        return records
    except Exception as e:
        print(f"   ❌ Error fetching {cloud_name}: {e}")
        return []

def fetch_tf_releases(repo):
    print(f"📦 [{repo}] Fetching Terraform Releases...")
    url = f"{GITHUB_API_BASE}/{repo}/releases"
    try:
        resp = requests.get(url, headers=HEADERS)
        if resp.status_code != 200:
            print(f"   ⚠️ GitHub API Error: {resp.status_code}")
            return []
            
        data = []
        for item in resp.json():
            data.append({
                "version": item.get('tag_name', 'v0.0.0'),
                "date": parser.parse(item['published_at']),
                "body": (item.get('body') or "").lower()
            })
        return data
    except Exception as e:
        print(f"   ❌ Error fetching releases: {e}")
        return []

def process_cloud(cloud_name, config):
    # 1. Get Cloud Features
    features = fetch_feed(cloud_name, config)
    if not features: return []

    # 2. Get TF Releases
    releases = fetch_tf_releases(config['tf_repo'])
    if not releases: return []

    # 3. Match
    print(f"⚙️ [{cloud_name.upper()}] Matching {len(features)} features against {len(releases)} releases...")
    
    for feat in features:
        # Sort releases by date
        releases.sort(key=lambda x: x['date'])
        
        # Only check releases AFTER the feature dropped
        valid_releases = [r for r in releases if r['date'] >= feat.ga_date]
        
        # Tokenize feature title
        raw_tokens = re.findall(r'\w+', feat.feature.lower())
        tokens = set(raw_tokens) - config['stop_words']
        
        best_match = None
        
        for release in valid_releases:
            hits = 0
            for t in tokens:
                if t in release['body']:
                    hits += 1
            
            score = hits / len(tokens) if tokens else 0
            
            if score > 0.45: # Strictness threshold
                best_match = release
                break
        
        if best_match:
            feat.tf_status = "Supported"
            feat.tf_version = best_match['version']
            lag = (best_match['date'] - feat.ga_date).days
            feat.lag_days = lag if lag >= 0 else 0
        else:
            feat.tf_status = "Not Supported"
            # Calc lag until today
            now = datetime.now(feat.ga_date.tzinfo)
            feat.lag_days = (now - feat.ga_date).days

    return [f.to_dict() for f in features]

def main():
    all_data = []
    
    for cloud, config in CLOUD_CONFIG.items():
        data = process_cloud(cloud, config)
        all_data.extend(data)
        time.sleep(1) # Be nice to APIs

    if not all_data:
        print("❌ No data collected from any cloud.")
        sys.exit(1)

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_data, f, indent=2)
    
    print(f"✅ Success! Wrote {len(all_data)} records (AWS/Azure/GCP) to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
