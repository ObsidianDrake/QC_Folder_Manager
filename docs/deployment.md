# 部署與維運

## 目標環境

- Web service：Linux 服務主機、RHEL 8.10、Python 3.9.10，不能使用 Docker。
- 執行／SSH 帳號：啟動 web service 的同一個 Linux 帳號；不需填入 username、不使用 sudo / su。
- 遠端 worker：需支援同帳號免密碼 SSH，並有一個 Python 3.9+ 直譯器執行純標準函式庫 helper。
  如果不在 PATH，設定 `hosts.remote-worker.ssh.python` 為該機器上的絕對路徑。
- Local 模式使用服務主機的本機 filesystem；同一 domain 不代表可直接使用 local 模式。

## 建置 binary

在 RHEL 8.10、相同 CPU 架構的建置主機執行（tcsh 亦可直接呼叫這些指令）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-build.txt
env PYTHON=.venv/bin/python bash scripts/build-rhel8.sh
.venv/bin/python tests/smoke_binary.py dist/qc-folder-manager
```

建置 Python 必須為 3.9.10；venv 需能取得相容的 Python shared library。
產物 `dist/qc-folder-manager` 與 SHA256 可複製到服務目錄；執行主機不需 pip 套件。
`make build PYTHON=.venv/bin/python` 可在其他 Linux 做開發驗證，不能據此宣稱 RHEL 相容。

## 設定

複製 `config.example.yml` 為 `config.yml`，填入正式的 public URL、listen address、port 與 data/log 目錄。
建議 SQLite 放在服務主機的本機持久化 filesystem，避免 NFS 上的 SQLite 鎖定問題。
同一份 state directory 只允許一個服務實例；不要以不同 state 目錄啟動多個管理相同 run root 的服務。

`server.public_url` 的來源是管理員設定，不信任任意 Host／Forwarded headers 決定 callback。
預設 listen 是 `127.0.0.1`，只接受本機或反向代理連線；直接對外測試時需自行調整 listen。
支援固定的 URL 子路徑，例如 `http://tools.example.com/qc_folder_mgt`；代理可以保留 `/qc_folder_mgt` 或移除後再轉送。
不要把範例 URL 當作已部署的網址。Traefik 設定與 callback 對應見 [Traefik 子路徑部署](traefik.md)。

### Gitea OAuth

在 Gitea 建立供 QC Folder Manager 專用的 OAuth application，設定：

```text
Redirect URI: <server.public_url>/auth/callback
```

預設 scopes 是 `read:user read:organization`。需允許查詢本人 `id`、`login`、`is_admin`
及本人組織清單（完整分頁）；公司 Gitea 的版本與 scope 行為在 UAT 確認。
不要共用 Drone 的 callback 或直接把 Gitea token 當成 Drone token。

環境變數使用現有 tcsh/csh 啟動習慣：

```tcsh
setenv QC_FOLDER_GITEA_CLIENT_ID "部署時填入"
setenv QC_FOLDER_GITEA_CLIENT_SECRET "部署時填入"
setenv QC_FOLDER_DRONE_TOKEN "部署時填入"
```

這些值只保存在服務帳號可讀的部署設定中，不提交 repository。
OAuth token 存於記憶體；重啟或到期後重新登入。無需額外 session secret，cookie 使用不可猜測的 opaque ID。
管理員資格只認 Gitea 網站 `is_admin`，不另維護 admin 帳號名單。

### SSH

保留服務帳號的 HOME、SSH config、既有私鑰／agent 與 known_hosts。
如果原本免密碼 SSH 依賴 ssh-agent，背景服務也必須有可用的 `SSH_AUTH_SOCK`。
程式固定使用目前有效 Linux 使用者，不受 SSH config 的 `User` 覆寫；
port、IdentityFile 等其餘既有設定繼續沿用。
未知 host key、認證失敗或沒有 remote Python 會回報失敗，不退回互動式密碼輸入。
遠端 shell 啟動檔若向 stdout 印出 banner，會破壞 JSON protocol，應只在互動 shell 印出。

### 檢查與啟動

```sh
./qc-folder-manager --config /path/to/config.yml --check-config
./qc-folder-manager --config /path/to/config.yml --preflight
./qc-folder-manager --config /path/to/config.yml
```

- `--check-config` 不進行連線或刪除。
- `--preflight` 查詢 Drone 服務帳號與每個 root／helper，不做刪除，也不能證明每個子檔案都有刪除權限。
- 啟動後的 `/health` 僅表示 HTTP 程序可用，不代表 Gitea、Drone、SSH 都正常。
- 使用既有服務管理器或背景程序管理方式啟動，保留相同 Linux 帳號。
  程序為前景服務，不自行 daemonize；不要另將 stdout/stderr 追加到永不輪替的檔案。
- PyInstaller one-file 會解壓到暫存目錄。設定服務專用、可寫且允許執行的 `TMPDIR`，
  不使用 QC root 或 SQLite data 目錄作暫存。強制 kill 後可能留下 `_MEI*`，停服務後才清理所屬殘留。

## Log rolling

```yaml
logging:
  directory: "/path/to/service/logs"
  max_bytes: 10485760
  backup_count: 5
```

每檔預設 10 MiB，保留目前檔與 5 個歷史檔：

| 檔案 | 用途 |
| --- | --- |
| `qc-folder-manager.log` | 目前寫入檔案 |
| `qc-folder-manager.log.1` | 最近一次備份 |
| `qc-folder-manager.log.2` … `.5` | 較舊備份，超過份數自動刪除 |

不需停止程序或外部 cron。設定修改後重新啟動生效；不要再對同一檔案套用外部 logrotate。
上限是依大小輪替，不是每日輪替。正常單筆訊息遠小於檔案上限，穩態總量約 60 MiB。
降低保留份數後，重啟會清除本服務超過新份數的編號備份；不刪除 logs 目錄裡的其他檔案。
手動加入的超大目前檔會在下一次寫入時輪替為備份，再依保留份數逐步淘汰。

一般 log 不保存 token、OAuth code、完整命令、request body 或例外 traceback。
連線失敗只記錄受控錯誤類別／errno；使用 operation ID 對照 SQLite 稽核與 UI。

## 稽核與容量

SQLite 位於 `<server.data_dir>/qc-folder-manager.sqlite3`，包含 inventory、scans、operations 與 append-only audit events。
Gitea ID／帳號、admin 身分與組織是提出刪除時的身分快照，執行前仍重新取得授權。
Vendor 不能透過 UI 修改或刪除 audit。一般成員只看所屬組織的紀錄，Gitea admin 可看全部納管紀錄。
稽核不隨 log rotation 清除；資料庫成長需另行監控。第一版不自動裁切稽核，避免失去刪除追蹤。

磁碟容量以 `st_blocks` 估算，掃描時會排除同一目錄樹內重複 hardlink 計數。
跨 run folder 的共享檔案、壓縮／reflink filesystem、仍被程序開啟的檔案，可能使預估值與實際釋放量不同；
UI 不把估計值宣稱為已釋放空間。

備份請使用 SQLite backup API 或停服務後複製完整資料庫；不可只複製仍在寫入的主檔而忽略 WAL。
服務帳號需有足夠空間保存 audit；若要求／running 紀錄無法寫入，平台不開始刪除。

## 異常清理核對

`unknown` 或 `partial` 不會自動重試。清理 helper 使用的中間目錄是：

```text
<run_folder>/.qcfm-delete-<operation-id>
```

1. 由管理員從 UI 查 operation ID、原始 host、完整路徑及結果。
2. 在同一 Linux 帳號下確認該 helper 已結束；SSH timeout 本身不能證明遠端已停止。
3. 核對原始目錄與對應的 `.qcfm-delete-*` 是否存在，確認有無重新建立的同名 run folder。
4. 對殘留內容做人工恢復或清理，避免操作新的同名執行資料。
5. 回到平台重新掃描；保留原始 unknown/partial 稽核作為事件紀錄。

第一版不提供「強制略過狀態」或「直接重試不明結果」按鈕，也不自動改寫歷史 outcome。
若同一紀錄持續被鎖定，應先完成上述人工核對再安排維護；不要以刪除 audit 資料列解除保護。

## UAT 項目

- 真實登入一般組織成員與 Gitea admin；確認組織分頁、admin 旗標及權限撤銷。
- 遠端 helper、SSH 環境、run root 與測試 build 對應。
- 在明確可丟棄的測試 run folder 驗證刪除及稽核；不要用正式未確認資料做首次刪除測試。
- 排隊中、執行中、等待相依／核准、API 故障時無法刪除。
- 服務重啟、SSH 斷線、磁碟無法寫 audit、log rotation 與 UI 子路徑。
- RHEL 8.10 的 binary、系統函式庫、UI 靜態檔與暫存目錄執行權限。

## 大型目錄與容量快取

開啟網頁與每 8 秒輪詢只讀取保存的 inventory，不觸發 filesystem 掃描。
「重新掃描」先列出 root 的直接子目錄，不遞迴計算容量；列表立即保存，Drone 狀態再逐筆更新。
新的紀錄（沒有容量快取）自動排入獨立的容量 worker，整個服務一次只遞迴計算一筆。
這避免容量工作佔滿掃描／刪除的工作執行緒，但仍可能與這些工作競爭 filesystem I/O。

同一 root／folder 的 device + inode 未變時保留舊容量，重新掃描不重算已有容量。
正在寫入的資料也可能使用舊快取，因此容量時間不是資料即時性的保證；需要最新值時按該列「更新容量」。
亦可勾選可清理紀錄後按「更新選取容量」（每次最多 100 筆）；受保護紀錄仍可使用逐列更新按鈕。
重算期間保留舊值，失敗不將容量歸零。總量只加總已知值，未完成或失敗項目需依各列狀態判讀。

結果以目錄身分與工作 ticket 比對才寫回，避免覆蓋已替換或已刪除的紀錄。
服務重啟會將未完成容量工作標示中斷；可手動重新更新。容量快取仍保留於 SQLite。
容量計算不鎖住整個專案，刪除前原有的完整目錄安全檢查仍保留，不能因為已有容量快取就略過。

## Disk Space

磁碟卡片以設定的 host ID 與實際 mount ID 分組；同一 host 上的多個 run root 若在相同 mount，合併顯示。
不同 host 不自動合併，即使指向同一 NFS。僅從登入者有權存取的專案建立卡片與專案清單。
磁碟數值代表整個 filesystem，不代表專案使用量或個別 quota；不提供跨磁碟加總。

Linux helper 依 root 的 fd 查詢 `/proc/self/mountinfo` 與 `fstatvfs`，不列舉 run folder 內容。
Available = `f_bavail * f_frsize`；Total = `f_blocks * f_frsize`；Used = `(f_blocks-f_bfree) * f_frsize`。
Used/Total 為使用率；有保留空間時 Available + Used 不一定等於 Total。

UI 輪詢時讀取記憶體快取，超過 60 秒才排入背景查詢；沒有瀏覽者時不定期刷新。
手動更新最短間隔 5 秒，同一專案已有查詢時不重複排入，磁碟查詢使用獨立的兩個 worker。
SSH disk 查詢 timeout 最長 20 秒；local filesystem 若由 kernel 卡住，仍可能占住該 worker，但不阻塞 HTTP 回應。
查詢失敗保留舊值並標示失敗，超過 90 秒或刪除後失效的數值標示過期。
刪除成功或部分刪除後重新查詢，不以 run folder 估計容量相減。
重啟後磁碟快取從空值開始重新查詢；不影響 SQLite 中的 run folder 容量與稽核。
