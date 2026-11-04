# QC Folder Manager

提供第三方 vendor 使用的 QC 執行紀錄與磁碟空間管理平台。

第一版已實作 Gitea OAuth 登入、組織／網站管理員權限、local／SSH 掃描、Drone build 保護、背景刪除、持久化稽核，以及自動 rolling log。
目標為 **RHEL 8.10 / Python 3.9.10**，不使用 Docker，提供 PyInstaller 單一 binary 建置流程。
公司內網連線、Gitea OAuth scopes 與 RHEL binary 實機相容性仍需 UAT；不將開發環境的 binary 當作正式產物。

## 介面語言

頁首提供繁體中文與 English 選單，登入前後均可切換。優先使用先前手動選擇的語言；首次開啟依瀏覽器偏好清單，選用第一個支援的中文或英文（所有中文變體顯示繁體中文），皆不支援時使用英文。手動選擇儲存在瀏覽器 localStorage；自動偵測結果不寫入，無法使用儲存空間時仍可於當次頁面切換。時間維持 Asia/Taipei，中英文皆顯示 YYYY/MM/DD HH:mm:ss。專案名稱、帳號與檔案路徑不翻譯。

## 使用流程

1. 使用 Gitea 帳號登入，頁面僅顯示可存取的專案。
2. 服務啟動後自動掃描所有納管專案；之後每個專案掃描完成（或失敗）後等待 `server.scan_interval_seconds` 再掃描，預設 300 秒。無人登入或瀏覽器關閉時仍會執行；Rescan 可立即要求掃描所選專案，同一專案不重複排隊。
3. 查看 run folder、build number、狀態、大小與 Drone 連結。Vendor 不會取得內部主機與完整路徑。
4. 選取已完成紀錄並確認刪除；每批最多 100 筆。
5. 在「刪除紀錄」查看實際 Gitea 操作人、目標、時間與結果。

| 身分 | 可操作範圍 | 執行中保護 |
| --- | --- | --- |
| 一般使用者 | 所屬組織的所有納管紀錄，不限原始 build 觸發者 | 適用 |
| Gitea 網站 admin | 所有設定檔納管的組織／專案 | 同樣適用，不能強制略過 |

管理員身分取自 Gitea 本人資料的 `is_admin`；不採用 organization owner、repository admin 或手動帳號白名單。
列表、稽核查詢、刪除請求及背景刪除開始前都檢查登入者的有效權限。

## 虛構設定範例

| 項目 | 值 |
| --- | --- |
| Gitea | `https://git.example.com/gitea` |
| Drone | `https://ci.example.com/drone` |
| Organization / repository | `ExampleVendor/Example_QC` |
| Host | `qc-worker.example.com`（zone-b） |
| Run root | `/srv/qc/example-project` |
| Prefix | `qc` |
| 虛構案例 | `qc_1234` 對應 build `1234` |

本 repository 僅使用虛構資料；正式設定存放於 Git 忽略的 `config.yml`，不可複製進文件或測試。

設定格式見 [config.example.yml](config.example.yml)。`hosts` 集中定義執行位置，`projects` 對應組織、repository、host、根目錄與 prefix。
新增專案只需加入設定；未設定的路徑不提供瀏覽或刪除。

- `transport: local`：直接操作服務所在主機可存取的 filesystem，不需要 SSH hostname。
- `transport: ssh`：使用服務程序的有效 Linux 帳號連線，不執行 sudo / su。
  沿用該帳號的 OpenSSH config、port、key / agent 與 known_hosts，未填密碼時使用 batch mode；也可在 `ssh.password` 直接填入密碼。
  `ssh.host_key_checking: accept-new` 會使用服務專用 known_hosts，自動保存首次金鑰並拒絕變更的金鑰。
- 遠端 helper 透過 stdin JSON 接收操作資料，不把使用者提供的路徑拼接為 shell command。
- **SSH 遠端主機需有 Python 3.9+**，只使用標準函式庫、不需 pip 套件。可用 `hosts.remote-worker.ssh.python` 指定既有直譯器絕對路徑。
  本機 binary 內含 Python 不代表遠端也有；用 `--preflight` 檢查。
- 相對的 data/log 路徑以設定檔位置解析，與目前工作目錄無關。

## 開發與建置

與 LSF Dashboard 相同，直接 `make` 即可自動建立 `.venv`、安裝應用程式依賴及 PyInstaller 並產生 binary，不需先 activate venv：

```sh
make
# make build 或 make executable 亦可
make test
make help
```

若 Python 不在 PATH，可執行 `make PYTHON=/path/to/python3`。
Makefile 自動建立並使用 `.venv`，安裝專案依賴及 PyInstaller；PyInstaller 不固定版本。
`make clean` 僅清除 `build/` 與 `dist/`，保留 venv、設定檔與資料。

產物：`dist/qc-folder-manager`。UI 資產與 SSH helper 會一併打包；設定檔與 SQLite 不放入 binary。

正式 RHEL 8.10 建置使用相同 CPU 架構的 RHEL 8.10 主機：

```sh
make release
```

此腳本確認 OS／Python 版本、執行測試、建置 binary 與檢查設定，輸出 SHA256。
建置時還需系統 `file`、`sha256sum` 及 Python shared library；不能用其他 Linux 上的 binary 直接宣稱相容 RHEL。
PyInstaller one-file 仍依賴相容系統函式庫及解壓暫存目錄；設定服務專用且允許執行的 `TMPDIR`。
參考 [PyInstaller operating mode](https://pyinstaller.org/en/stable/operating-mode.html)。

## 部署與啟動

完整步驟見 [部署與維運](docs/deployment.md)，Traefik 反向代理見 [子路徑部署](docs/traefik.md)。基本流程：

```sh
cp config.example.yml config.yml
./dist/qc-folder-manager --config config.yml --check-config
./dist/qc-folder-manager --config config.yml --preflight
./dist/qc-folder-manager --config config.yml
```

啟動前修改：

- `server.public_url`：使用者實際瀏覽的網址，允許子路徑，例如 `/qc_folder_mgt`。
- `server.listen`：預設 `127.0.0.1`，供同機反向代理使用。hostname 會綁定其解析的第一個地址；需指定介面時填 IP。
- `server.port`：省略、`null` 或 `0` 時，由 OS 分配並保留可用 port；填入 `1–65535` 則使用固定 port，已被占用時啟動失敗。實際 port 寫入啟動 log，`public_url` 不隨後端 port 改變。
- `traefik_api.enable`：預設 `false`。設為 `true` 後以 Bearer token 呼叫 [Traefik-API](https://github.com/ObsidianDrake/Traefik-API)，啟動時自動註冊，正常結束／SIGTERM／Ctrl+C 時移除。完整設定及關閉限制見 [自動註冊](docs/traefik.md#自動註冊與移除)。手動維護代理路由時請保留固定 `server.port`。
- `server.data_dir`：服務帳號可寫入的本機持久化目錄，必須在 QC 清理範圍之外。
- `logging.directory`：一般 log 存放位置，預設範例為 `./data/logs`。

使用獨立的 Gitea OAuth application，callback 為 `<server.public_url>/auth/callback`。
在 `config.yml` 直接填入憑證值，不需要設定環境變數；含真實憑證的設定檔不可提交 Git。

| 設定欄位 | 用途 |
| --- | --- |
| `gitea.client_id` | 此服務的 Gitea OAuth client ID |
| `gitea.client_secret` | 此服務的 Gitea OAuth client secret |
| `drone.token` | 能查詢全部納管 repositories 的 Drone 服務 token |

Vendor 不需輸入 Drone token 或 Linux 帳密。登入使用 OAuth state + PKCE，cookie 僅放隨機 session ID；登入取得的 OAuth token 存在服務記憶體，不放進 cookie、SQLite 或 log。
登出、服務重啟、session 到期或上游 token 失效後重新登入；第一版不持久化／自動刷新 OAuth token。
POST API 使用 CSRF token；所有資料查詢由後端依 Gitea 身分過濾。

## 刪除與失敗處理

- 只掃描 root 的直接子目錄，完整比對 `<prefix>_<正整數 build number>`。
  `qc_1234_backup`、`qc_001`、一般檔案、symlink 目錄不自動納管。
- 只允許已知終止狀態 `success`、`failure`、`error`、`killed`、`skipped`、`declined`。
  `pending`、`running`、`blocked`、`waiting_on_dependencies`、未知或 API 查詢失敗一律禁止。
- 執行狀態僅依 Drone；不查 LSF 或 OS process。刪除開始前再次查詢，但不宣稱跨系統原子性，仍有查核後立即重跑的時間差。
- 後端只接受 record ID。執行前比對 root／folder 的 device + inode，避免使用過期路徑。
  目錄樹以 fd-relative 操作、不跟隨 symlink，拒絕 nested mounts；共享檔案系統的 host aliases 由管理員避免重複設定。
- 先持久化工作與稽核，再進行刪除；SQLite 無法寫入時不開始刪除。
- Helper 先將目標移至同 root 下 `.qcfm-delete-<operation-id>` 再刪除內容，失敗保留待核對資料。
- SSH timeout 或服務中斷標記 `unknown`；部分刪除標記 `partial`，不自動重試。
  頁面顯示殘留清理目錄數量，請依 [異常清理核對](docs/deployment.md#異常清理核對)處理。
- 服務重啟時未執行的工作取消，曾開始執行的工作保留未知結果；不憑重啟判定成功。
- 只刪除實體 run folder；保留 Drone history 與平台稽核紀錄。

## Log rolling 與稽核

```yaml
logging:
  directory: "./data/logs"
  max_bytes: 10485760
  backup_count: 5
```

一般 log 自動依大小輪替為 `qc-folder-manager.log`、`.log.1` … `.log.5`，超過份數刪除最舊備份。
預設每檔 10 MiB，加上目前檔約 60 MiB 的穩態上限；log 單筆訊息不含 token、OAuth code、完整命令或 request body。
不需要停服務才能輪替，也不需額外 cron/logrotate。單服務實例用資料目錄的檔案鎖防止重複啟動。

**刪除稽核獨立存於 SQLite，不隨 log 輪替刪除。** 保存 Gitea ID／登入帳號／admin 身分、組織、目標完整路徑、build、時間與結果。
稽核資料預設不自動清除，因此 log 的容量上限不包含 SQLite；資料庫保留／備份政策另行規劃。

## 驗證

```sh
make test
.venv/bin/python tests/smoke_binary.py dist/qc-folder-manager
.venv/bin/python tests/smoke_route_lifecycle.py dist/qc-folder-manager
```

第二個指令啟動本機模擬 Gitea／Drone，透過 loopback SSH shim 執行真正 helper，測試打包後 OAuth、子路徑、UI 資產、掃描、刪除保護與稽核；不連公司主機。
第三個指令啟動隔離的 Traefik-API 模擬服務，驗證 binary 的自動／固定 port、註冊、SIGTERM／Ctrl+C 移除、停用模式與 API 錯誤處理；不修改正式 route。兩個 smoke script 也可傳入 `--source` 測試原始碼。
測試涵蓋跨組織隔離、管理員限制、權限改變、CSRF、inode 改變、symlink／mount 邊界、稽核失敗、重啟恢復、SSH 結果不明與 log 輪替。

[Drone 整合依據](docs/drone-integration.md)記錄 API 路由與狀態約定。
正式導入仍需在 UAT 驗證實際 OAuth、遠端 Python／SSH 權限、測試 build 對應與 RHEL binary。


## Vendor 資訊邊界

專案選單及摘要只顯示 organization / repository。Run record 保留資料夾名稱、build、
狀態與容量；刪除確認及操作歷史使用相同的業務識別資訊。
磁碟卡片保留容量與所屬專案，內部仍依 host / mount 分組，但不公開主機及掛載路徑。
一般工作區的所有登入者（含 admin）使用相同的瀏覽器回應欄位白名單：不回傳 host、environment、
完整路徑、mount、inode、SSH 設定、其他組織清單或原始診斷文字。
歷史 SQLite 紀錄亦在 API 出口過濾；完整操作稽核保留於伺服器資料庫，掃描診斷保留於 rolling log。
專案 ID、organization / repository、run folder 名稱與 Drone build 連結仍是可見資訊；
命名這些項目時應避免放入內部機密。

## 管理員專案設定

Gitea admin 可使用「專案管理」新增、編輯與移除專案；儲存會原子更新 `config.yml` 並立即生效。
可編輯 host（從既有設定選取）、run folder 與 prefix。ID、organization / repository 固定以保留歷史權限。
移除只停止納管，實體資料與歷史紀錄不刪除。一般 vendor 無法存取管理 API 或內部設定。
詳見 [部署文件](docs/deployment.md#管理員即時管理專案)。

## 自動掃描與容量時間

- `server.scan_interval_seconds`：60–86400 秒，預設 300；既有設定省略此欄位即啟用預設值。修改後重啟服務，不需重新建置。
- 排程由服務主程序啟動，不依賴登入或前端輪詢；`--check-config`／`--preflight` 不啟動排程。關閉服務時先停止排程，再等待已排入的工作完成。
- 管理頁新增／修改專案後，下次排程檢查自動掃描；移除的專案不再排程。排程與管理 API 共用設定鎖，掃描中不能修改該專案；設定需復原時暫停排程。
- 列表不再顯示 Last scanned 欄位及專案掃描完成時間；後端仍保留 `scanned_at` 供診斷，頁面保留尚未掃描、掃描中／失敗與待處理事項。每輪掃描會更新目錄清單及 Drone 狀態。
- **容量更新時間 / Size updated**：最近一次成功完成該資料夾遞迴容量計算的時間。不是目錄掃描時間。
- 自動與手動 Rescan 都保留已有容量快取，只為未知容量排入計算。需要重算已有容量時，使用該列或選取紀錄的容量更新按鈕；容量計算失敗時保留舊值與舊時間。
- 自動掃描沿用原本的刪除保護、資料隔離及服務憑證；不新增使用者權限，也不自動刪除任何資料。

## 刪除紀錄與保留期限

刪除工作以表格每頁顯示 50 筆，提供上一頁／下一頁；一般使用者依組織權限查閱。詳細稽核事件僅 admin 可見，API 對一般使用者回覆 403。瀏覽舊頁時不自動刷新，按更新紀錄回到第一頁。

後端啟動時及每小時清理超過 30 天的已結束刪除工作（依最後更新時間）與稽核事件（依事件時間）。queued、running、unknown、partial 工作及其稽核保留，避免遺失待處理線索。此清理只移除歷史資料，不刪除 run folder、Drone history 或專案設定稽核。SQLite 可重用釋出的頁面，不保證資料庫檔案立即縮小。

## 測試覆蓋率報告

執行 `make test`，或先 `python -m pip install -r requirements-dev.txt` 再執行 `python -m pytest`，皆會自動產生：

- `htmlcov/index.html`：可用瀏覽器開啟的 HTML 報告。
- `coverage.xml`：供 CI／品質分析工具讀取的 XML 報告。
- 終端機覆蓋率摘要與未覆蓋行號。

統計範圍為 `qcmanager` Python 程式碼，不包含前端 JavaScript，也不包含獨立執行的 binary smoke test。每次測試重新產生報告，報告與 `.coverage` 資料已加入 Git 忽略清單。
