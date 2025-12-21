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
        "urls": ["https://aws.amazon.com/about-aws/whats-new/recent/feed/"],
        "tf_repo": "hashicorp/terraform-provider-aws",
        "stop_words": {'amazon', 'aws', 'now', 'supports', 'available', 'introducing', 'general', 'availability', 'for', 'with', 'announcing'},
        "service_heuristic": "Amazon (.*?) "
    },
    "azure": {
        "urls": [
            "https://azurecomcdn.azureedge.net/en-us/updates/feed/", 
            "https://azure.microsoft.com/en-us/updates/feed/",       
            "https://azure.microsoft.com/en-us/blog/feed/"           
        ],
        "tf_repo": "hashicorp/terraform-provider-azurerm",
        "stop_words": {'azure', 'microsoft', 'public', 'preview', 'general', 'availability', 'now', 'available', 'support', 'in', 'generally', 'updates', 'update'},
        "service_heuristic": "Azure (.*?) "
    },
    "gcp": {
        "urls": ["https://cloud.google.com/feeds/gcp-release-notes.xml"],
        "tf_repo": "hashicorp/terraform-provider-google",
        "stop_words": {'google', 'cloud', 'platform', 'gcp', 'beta', 'ga', 'release', 'notes', 'available', 'support', 'feature', 'launch', 'new'},
        "service_heuristic": "^(.*?):" 
    }
}

# Synonyms to help matching (Marketing Name -> Technical Name)
SYNONYMS = {
    "compute engine": "compute",
    "kubernetes engine": "container",
    "cloud storage": "storage",
    "cloud sql": "sql",
    "cloud run": "cloudrun",
    "bigquery": "bigquery",
    "security command center": "scc",
    "vpc service controls": "vpcsc",
    "cloud functions": "cloudfunctions",
    "artifact registry": "artifactregistry"
}

GITHUB_API_BASE = "https://api.github.com/repos"
OUTPUT_FILE = "r2c_lag_data.json"

HEADERS = {
    'User-Agent': 'Rack2Cloud-Bot/1.2',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}

def make_aware(dt):
    """Force a datetime to be timezone-aware (UTC)."""
    if dt is None: return datetime.now(timezone.utc)
    if dt.tzinfo is None: return dt.replace(tzinfo=timezone.utc)
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
        clean_name = re.sub(r'[^a-zA-Z0-9]', '-', self.feature[:25]).lower()
        return {
            "id": f"{self.cloud}-{clean_name}",
            "cloud": self.cloud,
            "service": self.service[:25], 
            "feature": self.feature,
            "link": self.link,
            "status": self.tf_status,
            "version": self.tf_version,
            "lag": self.lag_days,
            "date": self.ga_date.strftime("%Y-%m-%d")
        }

def fetch_feed_with_failover(cloud_name, config):
    print(f"📡 [{cloud_name.upper()}] Fetching Cloud Feed...")
    valid_entries = []
    
    for url in config['urls']:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200: continue
            
            feed = feedparser.parse(resp.content)
            if not feed.entries: continue
                
            print(f"      ✅ Success! Found {len(feed.entries)} items.")
            valid_entries = feed.entries[:25] # Increased to 25
            break
        except:
            continue
            
    if not valid_entries:
        print(f"   ❌ ALL URLs FAILED for {cloud_name}.")
        return []

    records = []
    for entry in valid_entries:
        title = entry.title
        link = entry.link
        
        service = "General"
        match = re.search(config['service_heuristic'], title)
        if match:
            service = match.group(1).replace(",", "").strip()
        
        try:
            date_str = entry.get('updated', entry.get('published', entry.get('date')))
            dt = parser.parse(date_str)
        except:
            dt = datetime.now(timezone.utc)

        records.append(FeatureRecord(cloud_name, service, title, dt, link))
    return records

def fetch_tf_releases(repo):
    print(f"📦 [{repo}] Fetching Terraform Releases...")
    url = f"{GITHUB_API_BASE}/{repo}/releases"
    try:
        resp = requests.get(url, headers=HEADERS)
        if resp.status_code != 200: return []
        data = []
        for item in resp.json():
            try:
                dt = parser.parse(item['published_at'])
                data.append({
                    "version": item.get('tag_name', 'v0.0.0'),
                    "date": make_aware(dt), 
                    "body": (item.get('body') or "").lower()
                })
            except: continue
        return data
    except: return []

def process_cloud(cloud_name, config):
    features = fetch_feed_with_failover(cloud_name, config)
    if not features: return []

    releases = fetch_tf_releases(config['tf_repo'])
    
    if releases:
        releases.sort(key=lambda x: x['date'])
        for feat in features:
            valid_releases = [r for r in releases if r['date'] >= feat.ga_date]
            
            # --- IMPROVED MATCHING LOGIC ---
            raw_tokens = re.findall(r'\w+', feat.feature.lower())
            tokens = set(raw_tokens) - config['stop_words']
            
            # Check for Service Synonym (Boost factor)
            service_token = SYNONYMS.get(feat.service.lower(), feat.service.lower())
            
            best_match = None
            
            for release in valid_releases:
                hits = 0
                body = release['body']
                
                # 1. Does the service name appear in the changelog? (Big Boost)
                service_match = service_token in body
                
                for t in tokens:
                    if t in body: hits += 1
                
                # 2. Calculate Score
                score = hits / len(tokens) if tokens else 0
                
                # 3. Dynamic Thresholds
                # If service name is found, we accept a lower token match (0.25)
                # If not, we require a higher token match (0.35)
                threshold = 0.25 if service_match else 0.35
                
                if score >= threshold: 
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
            time.sleep(1)
        except Exception as e:
            print(f"❌ ERROR {cloud}: {e}")
            continue

    if not all_data:
        sys.exit(1)

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_data, f, indent=2)
    
    print(f"✅ Success! Wrote {len(all_data)} records.")

if __name__ == "__main__":
    main()
