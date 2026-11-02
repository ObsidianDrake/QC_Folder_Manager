# 部署與維運

## 目標環境

- Web service：Linux 服務主機、RHEL 8.10、Python 3.9.10，不能使用 Docker。
- 執行／SSH 帳號：啟動 web service 的同一個 Linux 帳號；不需填入 username、不使用 sudo / su。
- 遠端 worker：需支援同帳號 SSH（key / agent 或設定檔密碼），並有一個 Python 3.9+ 直譯器執行純標準函式庫 helper。
  如果不在 PATH，設定 `hosts.remote-worker.ssh.python` 為該機器上的絕對路徑。
- Local 模式使用服務主機的本機 filesystem；同一 domain 不代表可直接使用 local 模式。

## 建置 binary

在 RHEL 8.10、相同 CPU 架構的建置主機執行（tcsh 亦可直接呼叫這些指令）：

```sh
make release
# Python 不在 PATH 時：make release PYTHON=/path/to/python3
```

Makefile 自動建立 `.venv` 並安裝應用程式與建置依賴；不需 activate，也不需每次指定 venv Python。可用 `make help` 查看指令。
建置 Python 必須為 3.9.10；venv 需能取得相容的 Python shared library。
產物 `dist/qc-folder-manager` 與 SHA256 可複製到服務目錄；執行主機不需 pip 套件。
`make` 或 `make build` 可在其他 Linux 做開發驗證，不能據此宣稱 RHEL 相容。

PyInstaller 由 Makefile 直接執行 `pip install pyinstaller` 安裝，不在 requirements 固定版本。
應用程式與測試依賴分別由 `requirements.txt`、`requirements-dev.txt` 管理。

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

在 `config.yml` 的對應區段直接填入實際值（保留原有 `base_url` 等設定）：

```yaml
gitea:
  client_id: "replace-with-gitea-client-id"
  client_secret: "replace-with-gitea-client-secret"
drone:
  token: "replace-with-drone-token"
```

不需要 `setenv` 或 `export`。欄位名稱不再使用 `_env` 後綴。
設定檔只供服務帳號讀取（例如 `chmod 600 config.yml`），不可提交 repository。
缺少或填錯憑證欄位時，錯誤訊息會指出欄位名稱，不輸出憑證值。
OAuth token 存於記憶體；重啟或到期後重新登入。無需額外 session secret，cookie 使用不可猜測的 opaque ID。
管理員資格只認 Gitea 網站 `is_admin`，不另維護 admin 帳號名單。

### SSH

保留服務帳號的 HOME、SSH config、既有私鑰／agent 與 known_hosts。
如果原本免密碼 SSH 依賴 ssh-agent，背景服務也必須有可用的 `SSH_AUTH_SOCK`。
程式固定使用目前有效 Linux 使用者，不受 SSH config 的 `User` 覆寫；
port、IdentityFile 等其餘既有設定繼續沿用。
`ssh.password` 可直接填入該 Linux 帳號的密碼；省略時沿用 key / agent。
密碼模式透過 OpenSSH askpass 自動提供密碼，無需 sshpass、額外 Python 套件或圖形介面。
服務使用獨立 session，前景／背景執行皆不會等待終端機輸入密碼。
密碼不放入命令參數、log 或 helper 檔案；僅傳入該 SSH 子程序的環境。
askpass 暫存腳本放在 data_dir，該位置需允許執行腳本，結束或逾時後自動清除。

若系統設定 `UserKnownHostsFile /dev/null`，使用：

```yaml
ssh:
  python: "python3"
  password: "replace-with-linux-password"
  host_key_checking: "accept-new"
  timeout_seconds: 1800
```

`accept-new` 自動接受首次主機金鑰，保存於 `<server.data_dir>/ssh_known_hosts`，
覆蓋系統的 `/dev/null` 設定；已保存金鑰變更時仍拒絕連線。需要 OpenSSH 7.6+。
也可用 `ssh.known_hosts` 指定檔案，相對路徑以 config.yml 所在目錄為基準。
未設定 host_key_checking 時預設 `strict`，維持既有驗證方式。
更新設定後重啟服務；不需另外填寫 username，仍固定使用服務帳號。
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


### 掃描失敗診斷

UI 會依失敗原因顯示目錄不存在、存取權限不足、SSH 主機金鑰／連線問題、
遠端 Python 不存在或版本不符、命令逾時、回傳 JSON 無效等訊息。
在 `logging.directory/qc-folder-manager.log` 尋找 `scan failed diagnostic=`，
其後的 JSON 包含 project、stage、服務 user、host、transport、root、python，
以及可用的 SSH exit_code、timeout_seconds、錯誤類型與 errno_name。
非預期程式錯誤會保留檔名、行號、函式名稱，方便定位。

`ENOENT` 表示路徑不存在；`EACCES` 表示服務帳號無權存取；`python_missing`
表示遠端找不到設定的 Python。若回傳 JSON 無效，確認 shell 啟動檔沒有向 stdout 印出訊息。
SSH stderr 只用來分類已知錯誤，不原樣寫入 log；未知錯誤保留 exit_code。
不記錄完整 SSH 命令、helper 原始碼、任意例外內容或回傳本文。
診斷資訊仍使用既有 rolling log 容量限制。

### 管理員即時管理專案

以 Gitea 網站管理員登入後，開啟「專案管理」。可新增專案、修改既有專案的 host、run folder、prefix，
以及移除專案。Host 選單來自 `config.yml` 的 `hosts`；此頁不修改 host 連線或憑證，且不回傳任何密碼/token。
每次管理 API 都重新向 Gitea 驗證 `is_admin`，寫入另需目前登入 session 的 CSRF token。

UI 儲存會驗證完整設定，使用同目錄暫存檔與原子替換更新實際啟動時的 `config.yml`（權限 600），
隨即更新記憶體內的專案設定，無需重啟、重新 make 或重新登入。
服務帳號需要設定檔目錄的建立／替換檔案權限。YAML 會重新格式化，註解不會保留；其他設定值維持原樣。
不另外產生含憑證的設定備份。外部手動編輯設定檔仍須重啟；UI 偵測到外部變更會拒絕覆蓋。
多位管理員以 revision 檢查避免覆蓋彼此修改；遇到衝突請重新載入清單。

只有受到修改的專案必須沒有執行中／排隊的掃描、容量、磁碟查詢或刪除工作；其他專案可以繼續運作。
有 unknown／partial 清理結果的專案須先人工核對，不能直接移除或改路徑。
儲存成功後清除該專案的舊 inventory、scan 與 disk cache；重新掃描後才能再次刪除紀錄。
移除專案只停止納管，不刪除 run folder、Drone build 或歷史稽核。允許移除最後一個專案（`projects: []`）。

Project ID、organization、repository 是歷史紀錄的固定歸屬，建立後不可改成其他值；需改歸屬時建立新 ID。
刪除後也不能將同一 ID 轉交另一組織／repository。舊操作與稽核仍依原組織授權顯示，admin 可見全部歷史。
`project_catalog` 保存歸屬；`project_config_audit` 保存每次設定修改的 Gitea 操作者、時間、動作與前後專案設定。
這些資料存於既有 SQLite，管理頁不回傳 host 密碼或服務 token。
