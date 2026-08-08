# Kube Offline Arad

Docker Compose offline packager for **Kubespray**, modeled on
[`download-all.sh`](https://github.com/kubespray-offline/kubespray-offline/blob/develop/download-all.sh)
from [kubespray-offline](https://github.com/kubespray-offline/kubespray-offline).

It downloads **all Kubernetes/Kubespray requirements** (not just the playbooks):

| Stage | What |
|-------|------|
| Kubespray | Release tarball + `generate_list.sh` file/image lists |
| Files | kubeadm/kubectl/kubelet, etcd, CNI, crictl, containerd, runc, helm, … |
| Images | All Kubespray container images (+ `config/extra-images.txt`) |
| PyPI | Mirror of Kubespray `requirements.txt` wheels/sdists |
| OS repo | Ubuntu apt offline repo (`config/pkglist/ubuntu`) |
| Helm | Optional charts from `config/extra-charts.txt` |

Then **publishes** everything to:

1. **Docker Registry** (`:35000`) — container images  
2. **Nginx** (`:8080`) — files, Helm charts, PyPI, OS packages, and the
   Kubespray bundle under `data/outputs/`

All outbound downloads go through **V2Ray**. Local writes to nginx-backed
storage and pushes to the registry bypass the proxy.

## Quick start

```bash
./scripts/init.sh
# Edit config/v2ray.json with your real outbound
cp .env.example .env   # set public hosts to this machine's LAN IP

docker compose up -d --build
open http://localhost:8000          # control UI
open http://localhost:8080          # nginx artifact index
```

## Mattermost after-hours bot

An optional Compose service watches configured customer channels and posts a
threaded reply to every customer message received outside that channel's
contract hours. Messages from the bot, other integrations, system posts, and
the support-user IDs configured for that channel are ignored.

### 1. Prepare Mattermost

1. Create a Mattermost bot account and access token.
2. Add the bot to every customer channel it should watch. It needs permission
   to read the channel and create posts.
3. Record each channel ID and the Mattermost user IDs of your support staff.
   The API endpoints `GET /api/v4/channels/name/{team_name}/{channel_name}` and
   `GET /api/v4/users/username/{username}` can be used to look them up.

### 2. Prepare Google Sheets

Create a spreadsheet tab named `Support Hours` with this exact header. The
example below is tab-separated so it can be pasted directly into Sheets:

```text
enabled	channel_id	customer_name	timezone	support_days	open_time	close_time	support_24x7	support_user_ids	closed_message
TRUE	channel-id-acme	Acme	Asia/Tehran	Sat,Sun,Mon,Tue,Wed	09:00	17:00	FALSE	support-user-id-1,support-user-id-2	We are closed now. We will contact you on {next_open_date} at {next_open_time} ({timezone}).
TRUE	channel-id-global	Global Co	UTC	Mon,Tue,Wed,Thu,Fri,Sat,Sun	00:00	23:59	TRUE	support-user-id-1	This value is unused for 24x7 support.
```

- `timezone` must be an IANA name such as `Asia/Tehran` or `Europe/Berlin`.
- `support_days` accepts comma-separated English weekday names.
- One `open_time`/`close_time` pair is used for every selected day. The opening
  time is inclusive and closing time is exclusive. Overnight windows such as
  `18:00` to `02:00` are supported.
- Set `support_24x7` to `TRUE` to suppress all after-hours replies.
- `support_user_ids` is the comma-separated allowlist of staff whose messages
  must not trigger the bot.
- `closed_message` supports `{customer_name}`, `{next_open_date}`,
  `{next_open_time}`, and `{timezone}` placeholders.

Create a Google Cloud service account with read-only Sheets access, download
its JSON key to `config/google-service-account.json`, and share the spreadsheet
with the service account email as a Viewer.

### 3. Configure and run

Copy `.env.example` to `.env` and set:

```dotenv
MATTERMOST_URL=https://mattermost.example.com
MATTERMOST_BOT_TOKEN=your-bot-access-token
GOOGLE_SHEET_ID=the-id-between-d-and-edit-in-the-sheet-url
GOOGLE_SHEET_TAB=Support Hours
SHEET_REFRESH_SECONDS=300
```

Start only the optional bot, or include the profile when starting the full
stack:

```bash
docker compose --profile mattermost up -d --build mattermost-bot
docker compose logs -f mattermost-bot
```

The bot validates the complete sheet before applying a refresh. If Google
Sheets is temporarily unavailable or a row is invalid, it keeps using the last
valid rules cached in a Docker volume. On a first start with no valid rules it
sends no replies until a successful refresh.

Click **Start download-all**. The first core stage downloads files and Helm
charts into the nginx-mounted output tree, then pulls and pushes containers
into Docker Registry v2. PyPI and OS repositories follow.

## Pipeline vs kubespray-offline

| kubespray-offline | This project |
|-------------------|--------------|
| `get-kubespray.sh` | fetch Kubespray tarball |
| `pypi-mirror.sh` | `build_pypi_mirror()` |
| `download-kubespray-files.sh` | files + images via skopeo |
| `download-additional-containers.sh` | `config/extra-images.txt` |
| `create-repo.sh` | Ubuntu helper container + pkglist |
| `copy-target-scripts.sh` | `outputs/scripts/` |
| local registry / nginx | Docker Registry v2 + nginx |

## Where artifacts land

```
data/outputs/
  files/          # binaries for Kubespray files_repo
  images/         # docker-archive tars + images.list
  pypi/           # pip mirror
  debs/           # Ubuntu apt repo
  charts/         # helm chart tgz
  kubespray/      # kubespray-*.tar.gz
  scripts/        # air-gap helpers
  offline.yml     # group_vars for Kubespray
```

Nginx serves this directory directly, so files and charts do not need a
separate upload or publish step.

## Kubespray offline.yml

Generated at `data/outputs/offline.yml`:

- `registry_host: YOUR_IP:35000`
- `http_server: http://YOUR_IP:8080`

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/download` | Full download-all |
| POST | `/api/push` | Re-push image tars → Docker Registry |
| POST | `/api/v2ray` | Upload V2Ray JSON |
| GET | `/api/status` | Job log/status |
| GET | `/api/artifacts` | Counts + URLs |

## Notes

- Full image sets are **tens of GB**; give `data/` a large disk.
- OS repo build needs Docker socket mounted into `app` (already in compose).
- Change `UBUNTU_VERSION` (22.04 / 24.04) to match your target nodes.
