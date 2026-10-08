# 单微电网清退前归档

本归档冻结于 2026-10-05，包含当时全部源码（含未提交修改）、测试、配置、契约、工具及前端源文件，共 635 个文件。`manifest.json` 记录压缩包和每个文件的 SHA-256；`environment.txt` 记录依赖版本。源码包不含现场资料。

这是历史参考环境，不属于正式安装包。旧普通执行、旧条件监督、旧专项研究入口及其专用测试只在此处保留。更早的统一前版本仍保存在 `../single_execution_20261005`。

在项目根目录使用已安装归档依赖的解释器运行：

```powershell
.venv/Scripts/python.exe tools/replay_single_baseline.py --output artifacts/archive-regression
.venv/Scripts/python.exe tools/replay_single_baseline.py --read-result path/to/old-result.json --output artifacts/archive-result
```

工具先校验全部哈希，再将源码解到独立临时目录，通过子进程加载归档源码；不会恢复旧算法到正式源码树。读取结果只使用历史契约，不补造新版证据。历史完整研究可在独立目录解包、复核清单并按包内旧说明运行；已知的历史失败仍是失败。
