"""
Share links: a file on Google Drive, Dropbox, OneDrive, GitHub or Hugging Face.

Sharing a file there gives a link to a page that shows the file with a Download button, not
a link to the file.  Each site has its own way from that page to the file, so before
downloading idm takes it:

    drive.google.com/file/d/ID/view          drive.usercontent.google.com/download?id=ID&confirm=t
    docs.google.com/document/d/ID/edit       .../document/d/ID/export?format=docx  (Sheets: xlsx,
                                                                                   Slides: pptx)
    dropbox.com/scl/fi/.../data.zip?dl=0     the same with dl=1, named data.zip from the link
    1drv.ms/u/s!...                          OneDrive's own download link, with a guest pass
    github.com/o/r/blob/main/data.zip        github.com/o/r/raw/main/data.zip
    huggingface.co/o/r/blob/main/model.bin   huggingface.co/o/r/resolve/main/model.bin

A folder isn't one file: for a Drive folder idm lists the files in it to pick from, as it
does for a playlist.  The job keeps the link as it was given (`idm list` shows it, and a
resume is matched by it); only the requests go to the file's own link.
"""
import base64
import dataclasses
import json
import re
import urllib.parse
import urllib.request

from .errors import IDMError

DRIVE_FILE = "https://drive.usercontent.google.com/download"
DRIVE_FOLDER = "https://drive.google.com/embeddedfolderview?id={}"
DOCS_FORMAT = {"document": "docx", "spreadsheets": "xlsx", "presentation": "pptx"}  # the first
                                                                    # choice in File > Download
ONEDRIVE_PASS = "https://api-badgerp.svc.ms/v1.0/token"             # a guest pass, as OneDrive's
ONEDRIVE_APP = "5cbed6ac-a083-4e14-b191-b4ba07653de2"               # own page gets one
ONEDRIVE_ITEM = ("https://my.microsoftpersonalcontent.com/_api/v2.0/shares/u!{}/driveitem"
                 "?$select=name,folder,@content.downloadUrl")
SIGN_IN_HOSTS = ("accounts.google.com", "login.live.com", "login.microsoftonline.com")
FOLDER_MAX = 50                                                     # files listed from a folder


def the_file(opener, job):
    """The job, pointed at the file's own link when job.url is a share link (and named the
    way the site names the file, when its server doesn't say).  Other links: the job as is."""
    host = hostname(job.url)
    for site, find in SITES:
        if host == site or host.endswith("." + site):
            url, name = find(opener, job.url)
            return dataclasses.replace(job, url=url, name=job.name or name)
    return job


def sign_in_page(url):
    """Is this (where a link ended up) a page that asks the person to sign in?"""
    parts = urllib.parse.urlsplit(url)
    return hostname(url) in SIGN_IN_HOSTS or \
        bool(re.search(r"/(log-?in|sign-?in|ServiceLogin)\b", parts.path, re.I))


# ---- each site ----------------------------------------------------------------------------------
def google(opener, url):
    """A Drive file, a Drive folder, or a Google Doc, Sheet or Slides."""
    path, query = urllib.parse.urlsplit(url).path, query_of(url)
    doc = re.search(r"/(document|spreadsheets|presentation)/(?:u/\d+/)?d/([\w-]+)"
                    r"(?:/(?:edit|view|preview|copy|htmlview))?/?$", path)
    if doc and doc.group(2) != "e":            # the editor's link (a published one, /export,
        kind, doc_id = doc.groups()            # /pub or /gviz, already gives a file)
        return f"https://docs.google.com/{kind}/d/{doc_id}/export?format={DOCS_FORMAT[kind]}", None
    folder = re.search(r"/folders/([\w-]+)", path)
    folder_id = folder.group(1) if folder else path.endswith("folderview") and query.get("id")
    if folder_id:
        raise drive_folder(opener, folder_id)
    file = re.search(r"/file/d/([\w-]+)", path)
    old_style = re.fullmatch(r"(/u/\d+)?/(uc|open)", path)       # uc?id=ID, open?id=ID
    file_id = file.group(1) if file else old_style and query.get("id")
    if not file_id:
        return url, None
    extra = {"resourcekey": query["resourcekey"]} if query.get("resourcekey") else {}
    return DRIVE_FILE + "?" + urllib.parse.urlencode({"id": file_id, "export": "download",
                                                      "confirm": "t", **extra}), None


def drive_folder(opener, folder_id):
    """A Drive folder can't be downloaded as one file: NOT_A_FILE, with the links of the files
    (and folders) in it."""
    with opener.open(DRIVE_FOLDER.format(folder_id), timeout=30) as resp:
        if sign_in_page(resp.geturl()):
            return IDMError("AUTH_REQUIRED", "this Drive folder isn't shared with everyone")
        html = resp.read(2_000_000).decode("utf-8", "replace")
    found = re.findall(r'href="(https://drive\.google\.com/(?:file/d|drive/folders)/[\w-]+)', html)
    links = list(dict.fromkeys(found))[:FOLDER_MAX]
    return IDMError("NOT_A_FILE", f"this link is a Drive folder ({len(links)} items); "
                                  "idm downloads one file at a time", links=links)


def dropbox(opener, url):
    """dl=0 shows the file's page, dl=1 sends the file.  (Its server calls every file
    'unspecified', so a file link's own name is the name.)"""
    parts = urllib.parse.urlsplit(url)
    if parts.path.startswith("/scl/") and "rlkey" not in query_of(url):
        raise IDMError("BAD_URL", "this Dropbox link lost its rlkey=... part, which is what "
                                  "opens it to others: use the whole link")
    query = {**query_of(url), "dl": "1"}
    query.pop("raw", None)
    is_file = re.match(r"/(s|scl/fi)/", parts.path)
    name = urllib.parse.unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]) if is_file else None
    return parts._replace(query=urllib.parse.urlencode(query)).geturl(), name


def onedrive(opener, url):
    """OneDrive sends a shared file only to someone signed in, or with a guest pass, which
    its page gets for a visitor; idm gets one the same way and asks for the file's link."""
    req = urllib.request.Request(ONEDRIVE_PASS, data=json.dumps({"appId": ONEDRIVE_APP}).encode(),
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=30) as resp:
        guest = json.load(resp)["token"]
    share = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    req = urllib.request.Request(ONEDRIVE_ITEM.format(share),
                                 headers={"Authorization": f"Badger {guest}", "Prefer": "autoredeem"})
    with opener.open(req, timeout=30) as resp:
        item = json.load(resp)
    if "folder" in item:
        raise IDMError("NOT_A_FILE", "this link is a OneDrive folder; idm downloads one file at "
                                     "a time: ask the user for the link of the file itself")
    return item["@content.downloadUrl"], item.get("name")


def github(opener, url):
    """A file's page (/blob/) -> the file (/raw/)."""
    return re.sub(r"^(https://[^/]+/[^/]+/[^/]+)/blob/", r"\1/raw/", url), None


def hugging_face(opener, url):
    """A file's page (/blob/) -> the file (/resolve/), in a model (gpt2, or owner/model), a
    dataset or a space."""
    return re.sub(r"^(https://[^/]+/.+?)/blob/", r"\1/resolve/", url, count=1), None


SITES = (("drive.google.com", google), ("docs.google.com", google), ("dropbox.com", dropbox),
         ("1drv.ms", onedrive), ("onedrive.live.com", onedrive), ("github.com", github),
         ("huggingface.co", hugging_face))


def hostname(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def query_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
