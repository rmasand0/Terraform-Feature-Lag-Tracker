import requests
import feedparser
import json
import re
import os
import sys  # Added sys
from datetime import datetime
from dateutil import parser

# --- CONFIGURATION ---
GITHUB_API_URL = "https://api.github.com/repos/hashicorp/terraform-provider-aws/releases"
# Primary and Backup feeds
AWS_RSS_FEED = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'application/json'
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
        return {
            "id": re.sub(r'[^a-zA-Z0-9]', '-', f"{self.cloud}-{self.service}-{self.feature[:15]}").lower(),
            "cloud": self.cloud,
            "service": self.service,
            "feature": self.feature,
            "link": self.link,
            "status": self.tf_status,
            "version": self.tf_version,
            "lag": self.lag_days,
            "date": self.ga_date.strftime("%Y-%m-%d")
        }

def fetch_aws_data():
    print(f"📡 Fetching AWS Feed...")
    # Try parsing directly
    feed = feedparser.parse(AWS_RSS_FEED)
    
    # Validation: Check if feed has entries
    if not feed.entries:
        print("⚠️ AWS RSS Feed empty or blocked.")
        return []
        
    records = []
    for entry in feed.entries[:30]:
        title = entry.title
        link = entry.link
        
        service = "General"
        if "Amazon" in title:
            try:
                parts = title.split("Amazon ")
                if len(parts) > 1:
                    service = parts[1].split(" ")[0].replace(",", "")
            except:
                pass
        
        records.append(FeatureRecord(
            cloud="aws",
            service=service,
            feature=title,
            date=parser.parse(entry.published),
            link=link
        ))
    return records

def fetch_terraform_data():
    print("📦 Fetching Terraform Releases...")
    response = requests.get(GITHUB_API_URL, headers=HEADERS)
    if response.status_code != 200:
        print(f"⚠️ GitHub API Error: {response.status_code}")
        return []
    
    releases = []
    for item in response.json():
        releases.append({
            "version": item['tag_name'],
            "date": parser.parse(item['published_at']),
            "body": item['body'] or ""
        })
    return releases

def match_features(features, releases):
    print("⚙️ Matching Features...")
    for feat in features:
        valid_releases = [r for r in releases if r['date'] >= feat.ga_date]
        valid_releases.sort(key=lambda x: x['date'])
        
        feat_tokens = set(re.findall(r'\w+', feat.feature.lower())) - {'amazon', 'aws', 'now', 'supports', 'available', 'introducing'}
        
        best_match = None
        for release in valid_releases:
            body_lower = release['body'].lower()
            hits = sum(1 for t in feat_tokens if t in body_lower)
            score = hits / len(feat_tokens) if feat_tokens else 0
            
            if score > 0.4:
                best_match = release
                break
        
        if best_match:
            feat.tf_status = "Supported"
            feat.tf_version = best_match['version']
            lag = (best_match['date'] - feat.ga_date).days
            feat.lag_days = lag if lag >= 0 else 0
        else:
            feat.tf_status = "Not Supported"
            feat.lag_days = (datetime.now(feat.ga_date.tzinfo) - feat.ga_date).days

def main():
    try:
        features = fetch_aws_data()
        releases = fetch_terraform_data()
        
        # CRITICAL FIX: Exit with error if data is empty so Action fails
        if not features:
            print("❌ Error: No AWS features found. Check RSS Feed URL.")
            sys.exit(1)
            
        if not releases:
            print("❌ Error: No Terraform releases found. Check GitHub API.")
            sys.exit(1)

        match_features(features, releases)
        
        data_out = [f.to_dict() for f in features]
        
        with open("r2c_lag_data.json", 'w') as f:
            json.dump(data_out, f, indent=2)
            
        print(f"✅ Success! Wrote {len(data_out)} records to r2c_lag_data.json")
        
    except Exception as e:
        print(f"❌ Critical Exception: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
