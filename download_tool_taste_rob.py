# Script is modified from https://github.com/generalizable-neural-performer/gnr of Wei Cheng from HKUST.

import os
import re
import time
import json
import shutil
import base64
import zipfile
import requests
import argparse
from tqdm import tqdm
import urllib.request
from urllib import parse
import concurrent.futures
from requests.adapters import HTTPAdapter, Retry

# Ensure necessary packages are available
def import_or_install(package):
    try:
        __import__(package)
    except ImportError:
        import pip
        pip.main(["install", package])

import_or_install("urllib")
import_or_install("requests")
import_or_install("tqdm")
import_or_install("concurrent.futures")

# Enhanced header with more realistic browser behavior
header = {
    "sec-ch-ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "upgrade-insecure-requests": "1",
    "dnt": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
    "service-worker-navigation-preload": "true",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "iframe",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "cache-control": "max-age=0"
}

# Custom session with robust retry policy
def create_robust_session():
    s = requests.session()
    retries = Retry(
        total=10,
        backoff_factor=0.5,  # Exponential backoff: 0.5, 1, 2, 4, 8, etc. seconds
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

def get_file_size(file_path):
    if os.path.exists(file_path):
        return os.path.getsize(file_path)
    return 0

def parse_args():
    parser = argparse.ArgumentParser(description="Enhanced SharePoint Downloader")
    parser.add_argument(
        "--url", type=str, required=True, help="Download link from SharePoint"
    )
    parser.add_argument(
        "--download_folder",
        type=str,
        required=True,
        help="Path to store downloaded data"
    )
    parser.add_argument(
        "--force",
        type=bool,
        default=False,
        help="Force redownload even if file exists"
    )
    parser.add_argument(
        "--file_list",
        type=str,
        default=None,
        help="Path to text file listing specific files to download"
    )
    parser.add_argument(
        "--skip_first_n",
        type=int,
        default=0,
        help="Skip the first N files/folders"
    )
    parser.add_argument(
        "--max_retries",
        type=int,
        default=5,
        help="Maximum download retries per file"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Download timeout in seconds"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Number of parallel download threads"
    )
    return parser.parse_args()

def save_hash(path, code):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

def read_hash(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None

def check_and_download_file(session, download_url, local_file_path, force, 
                           max_retries, timeout, download_root):
    """Robust file download with retries and progress tracking"""
    if not force and os.path.exists(local_file_path):
        file_size = get_file_size(local_file_path)
        if file_size > 1024 * 100:  # 100KB threshold
            print(f"[SKIP] {os.path.relpath(local_file_path, download_root)} (exists)")
            return True
    
    for attempt in range(max_retries):
        try:
            print(f"[ATTEMPT {attempt+1}/{max_retries}] Downloading: {os.path.basename(local_file_path)}")
            response = session.get(download_url, stream=True, timeout=timeout, headers=header)
            response.raise_for_status()
            
            total_length = int(response.headers.get("content-length", 0))
            if total_length == 0:
                print(f"[ERROR] No content length, possible invalid link: {download_url}")
                return False
            
            with open(local_file_path, "wb") as f_dl, \
                 tqdm(
                     desc=os.path.relpath(local_file_path, download_root),
                     total=total_length,
                     unit="B",
                     unit_scale=True,
                     unit_divisor=1024,
                     leave=True
                 ) as bar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f_dl.write(chunk)
                        bar.update(len(chunk))
            
            print(f"[SUCCESS] Downloaded: {os.path.relpath(local_file_path, download_root)}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"[RETRY] Download failed: {str(e)}")
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + (random.random() * 1)  # Jittered backoff
                print(f"[WAIT] Retrying in {wait_time:.2f} seconds...")
                time.sleep(wait_time)
            else:
                print(f"[FAILURE] Max retries reached for {os.path.basename(local_file_path)}")
                if os.path.exists(local_file_path):
                    os.remove(local_file_path)
                return False
        except Exception as e:
            print(f"[ERROR] Unexpected error: {str(e)}")
            if os.path.exists(local_file_path):
                os.remove(local_file_path)
            return False

def process_subfolder(session, original_url, download_path, force, file_list_path, 
                     skip_first_n, download_root, layers):
    """Recursively process subfolders with enhanced error handling"""
    try:
        print(f"LAYER {layers}: Fetching folder: {original_url}")
        response = session.get(original_url, headers=header, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"LAYER {layers}: Failed to fetch folder: {str(e)}")
        return 0

    redirect_url = response.url
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(redirect_url).query))
    print(f"LAYER {layers}: Redirected to: {redirect_url}")

    context_info = None
    form_digest_value = None
    site_absolute_url = None
    list_url_from_context = None

    match_context = re.search(r"var _spPageContextInfo\s*=\s*({.*?});", response.text, re.DOTALL)
    if match_context:
        try:
            context_info_json = json.loads(re.sub(r",\s*([}\]])", r"\1", match_context.group(1)))
            form_digest_value = context_info_json.get("formDigestValue")
            site_absolute_url = context_info_json.get("webAbsoluteUrl")
            list_url_from_context = context_info_json.get("listUrl")
            print(f"LAYER {layers}: Extracted siteAbsoluteUrl: {site_absolute_url}")
        except Exception as e:
            print(f"LAYER {layers}: Error parsing context info: {str(e)}")

    current_folder = query.get("id")
    if not current_folder:
        print(f"LAYER {layers}: Error: No folder ID in query")
        return 0

    list_server_relative_url = list_url_from_context or current_folder
    if not list_server_relative_url:
        print(f"LAYER {layers}: Error: Cannot determine list URL")
        return 0

    # Prepare GraphQL query
    encoded_list = parse.quote(list_server_relative_url)
    encoded_folder = parse.quote(current_folder)
    graphql_query = (
        "query ($listServerRelativeUrl: String!, $renderListDataAsStreamParameters: RenderListDataAsStreamParameters!, "
        "$renderListDataAsStreamQueryString: String!) { "
        "legacy { renderListDataAsStream(listServerRelativeUrl: $listServerRelativeUrl, "
        "parameters: $renderListDataAsStreamParameters, queryString: $renderListDataAsStreamQueryString) } "
        "perf { executionTime } }"
    )
    graphql_vars = {
        "listServerRelativeUrl": list_server_relative_url,
        "renderListDataAsStreamParameters": {
            "renderOptions": 5707527,
            "folderServerRelativeUrl": current_folder,
            "addRequiredFields": True
        },
        "renderListDataAsStreamQueryString": f"@a1='{encoded_list}'&RootFolder={encoded_folder}&TryNewExperienceSingle=TRUE"
    }
    graphql_payload = json.dumps({"query": graphql_query, "variables": graphql_vars})

    graphql_headers = {
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
        "User-Agent": header["user-agent"],
        "Referer": redirect_url
    }
    if form_digest_value:
        graphql_headers["X-RequestDigest"] = form_digest_value

    graphql_endpoint = f"{site_absolute_url.rstrip('/')}/_api/v2.1/graphql" if site_absolute_url else None
    if not graphql_endpoint:
        print(f"LAYER {layers}: Error: Cannot determine GraphQL endpoint")
        return 0

    try:
        graphql_response = session.post(
            graphql_endpoint, data=graphql_payload.encode("utf-8"), 
            headers=graphql_headers, timeout=30
        )
        graphql_response.raise_for_status()
        graphql_data = graphql_response.json()
    except Exception as e:
        print(f"LAYER {layers}: Error in GraphQL request: {str(e)}")
        return 0

    if "errors" in graphql_data:
        print(f"LAYER {layers}: GraphQL errors: {graphql_data['errors']}")
        return 0

    list_data = graphql_data.get("data", {}).get("legacy", {}).get("renderListDataAsStream", {})
    if not list_data:
        print(f"LAYER {layers}: No list data found")
        return 0

    all_items = []
    current_node = list_data.get("ListData")
    if current_node and "Row" in current_node:
        all_items.extend(current_node["Row"])

    # Handle pagination
    while "NextHref" in current_node:
        next_href = current_node["NextHref"]
        next_url = f"{graphql_endpoint}{next_href}&@a1='{encoded_list}'&TryNewExperienceSingle=TRUE"
        
        try:
            pagination_response = session.post(
                next_url, data=json.dumps({
                    "parameters": {
                        "__metadata": {"type": "SP.RenderListDataParameters"},
                        "RenderOptions": 1216519,
                        "AddRequiredFields": True
                    }
                }).encode("utf-8"),
                headers=graphql_headers, timeout=30
            )
            pagination_data = pagination_response.json()
            current_node = pagination_data.get("ListData")
            if current_node and "Row" in current_node:
                all_items.extend(current_node["Row"])
        except Exception as e:
            print(f"LAYER {layers}: Pagination error: {str(e)}")
            break

    os.makedirs(download_path, exist_ok=True)

    # Process file list if provided
    selected_files = set()
    if file_list_path and os.path.exists(file_list_path):
        try:
            with open(file_list_path, "r", encoding="utf-8") as f:
                selected_files = {line.strip() for line in f if line.strip()}
            print(f"LAYER {layers}: Selected {len(selected_files)} files to download")
        except Exception as e:
            print(f"LAYER {layers}: Error reading file list: {str(e)}")

    # Sort items to process folders first
    all_items.sort(key=lambda x: x.get("FSObjType", "0"))  # Folders (FSObjType=1) first

    downloaded_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
        for item in all_items:
            file_ref = item.get("FileRef", "")
            file_leaf = item.get("FileLeafRef", "")
            if not file_leaf:
                continue

            is_folder = item.get("FSObjType") == "1"
            file_path = os.path.join(download_path, file_leaf)
            rel_path = os.path.relpath(file_path, download_root)

            if is_folder:
                # Skip if not in selected folders
                if selected_files and file_leaf not in {f.split('/')[-2] for f in selected_files}:
                    continue
                
                print(f"\nLAYER {layers}: Processing subfolder: {file_leaf}")
                subfolder_url = f"{redirect_url.split('?')[0]}?{parse.urlencode({**query, 'id': os.path.join(current_folder, file_leaf)})}"
                subfolder_path = os.path.join(download_path, file_leaf)
                
                # Recursive call for subfolder
                subfolder_result = process_subfolder(
                    session, subfolder_url, subfolder_path, force, 
                    file_list_path, skip_first_n, download_root, layers + 1
                )
                downloaded_count += subfolder_result
            else:
                # Check if file is selected
                full_path = f"{os.path.dirname(file_ref).split('/')[-1]}/{file_leaf}"
                if selected_files and full_path not in selected_files:
                    continue

                download_url = item.get("@content.downloadUrl")
                if not download_url:
                    sp_item_url = item.get(".spItemUrl")
                    if sp_item_url:
                        try:
                            meta_resp = session.get(sp_item_url, headers=header, timeout=10)
                            meta_data = meta_resp.json()
                            download_url = meta_data.get("@content.downloadUrl")
                        except Exception as e:
                            print(f"LAYER {layers}: Error fetching download URL for {file_leaf}: {str(e)}")
                            continue
                    else:
                        print(f"LAYER {layers}: No download URL for {file_leaf}")
                        continue

                if not download_url:
                    print(f"LAYER {layers}: Skipping {file_leaf} (no valid download URL)")
                    continue

                # Submit download task to thread pool
                future = executor.submit(
                    check_and_download_file, session, download_url, file_path, 
                    force, args.max_retries, args.timeout, download_root
                )
                if future.result():
                    downloaded_count += 1

    return downloaded_count

if __name__ == "__main__":
    import random
    args = parse_args()
          
    print(f"Starting download: URL={args.url}, Folder={args.download_folder}")
    print(f"Options: Force={args.force}, Retries={args.max_retries}, Timeout={args.timeout}s, Parallel={args.parallel}")
    
    # Create robust session with retries
    session = create_robust_session()
    
    # Start recursive download
    total_downloaded = process_subfolder(
        session, args.url, args.download_folder, args.force, 
        args.file_list, args.skip_first_n, args.download_folder, 0
    )
    
    print(f"\nDownload completed! Total files downloaded: {total_downloaded}")