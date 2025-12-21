import requests
import feedparser
import json
import re
import sys
import time
from datetime import datetime, timezone
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
        # Using the direct Microsoft feed which is often less blocked than the CDN
        "rss": "https://azure.microsoft.com/en-us/updates/feed/",
        "tf_repo": "hashicorp/terraform-provider-azurerm",
        "stop_words": {'azure', 'microsoft', 'public', 'preview', 'general', 'availability', 'now', 'available', 'support', 'in', 'generally'},
        "service_heuristic": "Azure (.*?) "
    },
    "gcp": {
        "rss": "https://cloud.google.com/feeds/gcp-release-notes.xml",
        "tf_repo": "hashicorp/terraform-provider-google",
        "stop_words": {'google', 'cloud', 'platform', 'gcp', 'beta', 'ga', 'release', 'notes', 'available', 'support'},
        "service_heuristic": "^(.*?):" 
    }
}

GITHUB_API_BASE = "https://api.github.com/repos"
OUTPUT_FILE = "r2c_lag_data.json"

# SPOOFED HEADERS (Looks like Chrome Browser to bypass Azure blocks)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, application/xml, text/xml, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}

def make_aware(dt):
    """Force a datetime to be timezone-aware (UTC) to prevent crashes."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

class FeatureRecord:
    def __init__(self, cloud, service, feature, date, link):
        self.cloud = cloud
        self.service = service
        self.feature = feature
        self.ga_date = make_aware(date)
        self.link = link
        self.tf_status = "Not Supported"
        self.tf_version = "--"
        self.lag_days = 0

    def to_dict(self):
        # Create unique ID
        clean_name = re.sub(r'[^a-zA-Z0-9]', '-', self.feature[:20]).lower()
        return {
            "id": f"{self.cloud}-{clean_name}",
            "cloud": self.cloud,
            "service": self.service[:20], 
            "feature": self.feature,
            "link": self.link,
            "status": self.tf_status,
            "version": self.tf_version,
            "lag": self.lag_days,
            "date": self.ga_date.strftime("%Y-%m-%d")
        }

def fetch_feed_content(url):
    """Manual fetch with Headers to bypass blocks, then pass to feedparser."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.content
        print(f"   ⚠️ HTTP Error {resp.status_code} fetching feed.")
    except Exception as e:
        print(f"   ⚠️ Connection Error: {e}")
    return None

def fetch_feed(cloud_name, config):
    print(f"📡 [{cloud_name.upper()}] Fetching Cloud Feed...")
    
    # 1. Fetch raw content first (to apply Headers)
    raw_xml = fetch_feed_content(config['rss'])
    if not raw_xml:
        return []

    # 2. Parse
    feed = feedparser.parse(raw_xml)
    
    if not feed.entries:
        print(f"   ⚠️ Warning: {cloud_name} feed parsed as empty.")
        return []
        
    records = []
    # Process last 20 entries
    for entry in feed.entries[:20]:
        title = entry.title
        link = entry.link
        
        # Service Logic
        service = "General"
        match = re.search(config['service_heuristic'], title)
        if match:
            service = match.group(1).replace(",", "").strip()
        
        # Date Logic (Robust)
        try:
            # Prefer 'updated', fall back to 'published'
            date_str = entry.get('updated', entry.get('published'))
            dt = parser.parse(date_str)
        except:
            dt = datetime.now(timezone.utc)

        records.append(FeatureRecord(
            cloud=cloud_name,
            service=service,
            feature=title,
            date=dt,
            link=link
        ))
    
    print(f"   ✅ Found {len(records)} items.")
    return records

def fetch_tf_releases(repo):
    print(f"📦 [{repo}] Fetching Terraform Releases...")
    url = f"{GITHUB_API_BASE}/{repo}/releases"
    try:
        resp = requests.get(url, headers=HEADERS)
        if resp.status_code != 200:
            return []
            
        data = []
        for item in resp.json():
            try:
                dt = parser.parse(item['published_at'])
                data.append({
                    "version": item.get('tag_name', 'v0.0.0'),
                    "date": make_aware(dt), 
                    "body": (item.get('body') or "").lower()
                })
            except:
                continue
        return data
    except Exception:
        return []

def process_cloud(cloud_name, config):
    features = fetch_feed(cloud_name, config)
    if not features: return []

    releases = fetch_tf_releases(config['tf_repo'])
    
    # Matching Logic
    if releases:
        releases.sort(key=lambda x: x['date'])
        
        for feat in features:
            valid_releases = [r for r in releases if r['date'] >= feat.ga_date]
            
            raw_tokens = re.findall(r'\w+', feat.feature.lower())
            tokens = set(raw_tokens) - config['stop_words']
            
            best_match = None
            for release in valid_releases:
                hits = 0
                for t in tokens:
                    if t in release['body']:
                        hits += 1
                
                # Dynamic Score: Need more matches for long titles
                score = hits / len(tokens) if tokens else 0
                if score > 0.45: 
                    best_match = release
                    break
            
            if best_match:
                feat.tf_status = "Supported"
                feat.tf_version = best_match['version']
                lag = (best_match['date'] - feat.ga_date).days
                feat.lag_days = lag if lag >= 0 else 0
            else:
                feat.tf_status = "Not Supported"
                now = datetime.now(timezone.utc)
                feat.lag_days = (now - feat.ga_date).days

    return [f.to_dict() for f in features]

def main():
    all_data = []
    
    for cloud, config in CLOUD_CONFIG.items():
        try:
            data = process_cloud(cloud, config)
            all_data.extend(data)
            time.sleep(1) # Polite delay
        except Exception as e:
            print(f"❌ CRITICAL ERROR processing {cloud}: {e}")
            continue

    if not all_data:
        print("❌ No data collected from any cloud.")
        sys.exit(1)

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_data, f, indent=2)
    
    print(f"✅ Success! Wrote {len(all_data)} total records to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
