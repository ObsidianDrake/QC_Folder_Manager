# QC Folder Manager

提供第三方 vendor 使用的 QC 執行紀錄與磁碟空間管理平台。

第一版已實作 Gitea OAuth 登入、組織／網站管理員權限、local／SSH 掃描、Drone build 保護、背景刪除、持久化稽核，以及自動 rolling log。
目標為 **RHEL 8.10 / Python 3.9.10**，不使用 Docker，提供 PyInstaller 單一 binary 建置流程。
公司內網連線、Gitea OAuth scopes 與 RHEL binary 實機相容性仍需 UAT；不將開發環境的 binary 當作正式產物。

## 介面語言

頁首提供繁體中文與 English 選單，登入前後均可切換。預設繁體中文；語言偏好儲存在瀏覽器 localStorage，無法使用儲存空間時仍可於當次頁面切換。時間維持 Asia/Taipei，依語言調整顯示格式。專案名稱、帳號與檔案路徑不翻譯。

## 使用流程

1. 使用 Gitea 帳號登入，頁面僅顯示可存取的專案。
2. 選擇專案並重新掃描；容量與 Drone 狀態在背景取得。
3. 查看 run folder、build number、狀態、大小、host、路徑與 Drone 連結。
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
  沿用該帳號的 OpenSSH config、port、key / agent 與 known_hosts，強制 batch mode 及 host key 驗證。
- 遠端 helper 透過 stdin JSON 接收操作資料，不把使用者提供的路徑拼接為 shell command。
- **SSH 遠端主機需有 Python 3.9+**，只使用標準函式庫、不需 pip 套件。可用 `hosts.remote-worker.ssh.python` 指定既有直譯器絕對路徑。
  本機 binary 內含 Python 不代表遠端也有；用 `--preflight` 檢查。
- 相對的 data/log 路徑以設定檔位置解析，與目前工作目錄無關。

## 開發與建置

在 Python 3.9.10 環境建立 virtualenv 並安裝固定版本套件：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-build.txt
make test PYTHON=.venv/bin/python
make build PYTHON=.venv/bin/python
```

產物：`dist/qc-folder-manager`。UI 資產與 SSH helper 會一併打包；設定檔與 SQLite 不放入 binary。

正式 RHEL 8.10 建置使用相同 CPU 架構的 RHEL 8.10 主機：

```sh
env PYTHON=.venv/bin/python bash scripts/build-rhel8.sh
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
- `server.listen`／`server.port`：預設 `127.0.0.1:9080`，供反向代理使用。範例 public URL 不是已部署的網址。
- `server.data_dir`：服務帳號可寫入的本機持久化目錄，必須在 QC 清理範圍之外。
- `logging.directory`：一般 log 存放位置，預設範例為 `./data/logs`。

使用獨立的 Gitea OAuth application，callback 為 `<server.public_url>/auth/callback`。
服務端環境變數（不可提交 Git）：

| 環境變數 | 用途 |
| --- | --- |
| `QC_FOLDER_GITEA_CLIENT_ID` | 此服務的 Gitea OAuth client ID |
| `QC_FOLDER_GITEA_CLIENT_SECRET` | 此服務的 Gitea OAuth client secret |
| `QC_FOLDER_DRONE_TOKEN` | 能查詢全部納管 repositories 的 Drone 服務 token |

Vendor 不需輸入 Drone token 或 Linux 帳密。登入使用 OAuth state + PKCE，cookie 僅放隨機 session ID；token 存在服務記憶體，不放進 cookie、SQLite 或 log。
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
make test PYTHON=.venv/bin/python
.venv/bin/python tests/smoke_binary.py dist/qc-folder-manager
```

第二個指令啟動本機模擬 Gitea／Drone，透過 loopback SSH shim 執行真正 helper，測試打包後 OAuth、子路徑、UI 資產、掃描、刪除保護與稽核；不連公司主機。
測試涵蓋跨組織隔離、管理員限制、權限改變、CSRF、inode 改變、symlink／mount 邊界、稽核失敗、重啟恢復、SSH 結果不明與 log 輪替。

[Drone 整合依據](docs/drone-integration.md)記錄 API 路由與狀態約定。
正式導入仍需在 UAT 驗證實際 OAuth、遠端 Python／SSH 權限、測試 build 對應與 RHEL binary。
