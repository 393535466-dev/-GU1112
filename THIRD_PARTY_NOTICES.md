# Third-party notices

This project adapts selected code from [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis/tree/ef44660f00e08ce66dcd4dc4d2e700836c45e833), commit `ef44660f00e08ce66dcd4dc4d2e700836c45e833`. The upstream MIT copyright and license are in [LICENSE](LICENSE). The trading calendar and market symbol helpers in `lazy_picker/engine/` are from this project.

The market temperature calculation in `lazy_picker/market.py` adapts DSA's Market Light scoring from `src/market_analyzer.py`.

The observation plans in `lazy_picker/advice.py` adapt DSA's MA5/MA10/MA20 alignment and MA5 bias rules from `src/stock_analyzer.py` and `src/agent/skills/defaults.py`.

The upstream screening files and strategy YAML templates are derived from [AlphaSift](https://github.com/ZhuLinsen/alphasift/tree/9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf), revision `9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf`, under Apache License 2.0. Their original notices and [Apache license](lazy_picker/engine/LICENSE) are included. Python imports in the copied modules have been adapted for this standalone package.
