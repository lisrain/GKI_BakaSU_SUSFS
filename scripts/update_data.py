"""增量更新 GKI 内核版本数据。"""

import copy
import json
import os
import sys
import time

from gki_fetch import (
    TARGETS,
    fetch_lts,
    fetch_latest_release_tags,
    fetch_makefile,
    fetch_monthly_branches,
    fetch_tag_makefile,
    get_end_date,
    json_path,
    make_date_range,
    parse_version,
)
from prepare_matrix import validate_data


def update_target(android_ver: str, kernel_ver: str,
                  date_start: str, date_end: str | None,
                  dep_cutoff: str) -> bool:
    path = json_path(android_ver, kernel_ver)
    end = get_end_date(date_end)
    is_k510 = (kernel_ver == "5.10")
    has_release_tags = kernel_ver in ("5.10", "5.15")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        validate_data(data, path, android_ver, kernel_ver)
        entries = data.get("entries", [])
    else:
        data = {
            "android_version": android_ver,
            "kernel_version": kernel_ver,
            "entries": [],
        }
        if not is_k510:
            data["lts"] = None
        entries = []
    original_data = copy.deepcopy(data)

    existing_by_date = {e["date"]: e for e in entries}
    all_dates = make_date_range(date_start, end)
    if has_release_tags:
        release_tags = fetch_latest_release_tags(android_ver, kernel_ver)
        monthly_branches = fetch_monthly_branches(android_ver, kernel_ver)
        for date in all_dates:
            release = release_tags.get(date)
            tag = release[0] if release is not None else None
            current = existing_by_date.get(date)
            previous = current.copy() if current is not None else None
            if tag is None:
                if date not in monthly_branches:
                    if current is not None:
                        raise RuntimeError(f"no release tag or branch for {android_ver}-{kernel_ver}-{date}")
                    continue
                text = fetch_makefile(android_ver, kernel_ver, date, dep_cutoff)
                if text is None:
                    # AOSP 会在发布日期到来之前就把当月分支的占位 ref 建好：它指向
                    # kernel/common 的空初始提交，树为空、没有任何文件，所以两个
                    # 路径的 Makefile 都是 404。这不是上游故障，只是这个月还没发布。
                    # 反过来说，如果这个月本来就有数据，说明分支此前确实有内容，
                    # 那才是需要报出来的异常。
                    if current is not None:
                        raise RuntimeError(
                            f"monthly branch has no Makefile: {android_ver}-{kernel_ver}-{date}"
                        )
                    print(f"  [{date}] branch not released yet, skip")
                    continue
            else:
                text = fetch_tag_makefile(*release)
            ver = parse_version(text)
            if ver is None:
                raise RuntimeError(f"failed to parse Makefile for {tag or date}")
            detail = ".".join(ver)
            if current is None:
                entry = {"date": date, "kernel": detail}
                entries.append(entry)
                existing_by_date[date] = entry
                current = entry
            else:
                current["kernel"] = detail
            if tag is None:
                current.pop("revision", None)
            else:
                current["revision"] = "r" + tag.rsplit("_r", 1)[1]
            if current != previous:
                print(f"  [{tag or date}] -> {detail}")
            time.sleep(0.3)
    else:
        new_dates = [date for date in all_dates if date not in existing_by_date]
        if not new_dates:
            print("  No new months to fetch")
        monthly_branches = (
            fetch_monthly_branches(android_ver, kernel_ver) if new_dates else set()
        )
        for date in new_dates:
            label = f"{android_ver}-{kernel_ver}-{date}"
            print(f"    [{label}] ", end="", flush=True)
            if date not in monthly_branches:
                print("not found, skip")
                continue
            text = fetch_makefile(android_ver, kernel_ver, date, dep_cutoff)
            if text is None:
                print("not found, skip")
                continue
            ver = parse_version(text)
            if ver is None:
                raise RuntimeError(f"failed to parse Makefile for {label}")
            detail = ".".join(ver)
            entries.append({"date": date, "kernel": detail})
            print(f"-> {detail}")
            time.sleep(0.3)

    # 排序：将具体日期按字母排序，'lts' 置于末尾
    entries.sort(key=lambda e: (e.get("date") == "lts", e.get("date", "")))

    # 更新 LTS
    lts_label = f"{android_ver}-{kernel_ver}-lts"
    print(f"  [{lts_label}] ", end="", flush=True)
    lts_text = fetch_lts(android_ver, kernel_ver)
    if lts_text is None:
        raise RuntimeError(f"LTS branch not found: {lts_label}")

    ver = parse_version(lts_text)
    if ver is None:
        raise RuntimeError(f"failed to parse Makefile for {lts_label}")

    version, patchlevel, sublevel = ver
    lts_value = f"{version}.{patchlevel}.{sublevel}"

    if is_k510:
        # 5.10 的 LTS 存在于 entries 内
        lts_entry = next((e for e in entries if e.get("date") == "lts"), None)
        if lts_entry:
            if lts_entry.get("kernel") != lts_value:
                lts_entry["kernel"] = lts_value
                print(f"-> {lts_value} (updated entries.lts)")
            else:
                print(f"-> {lts_value} (unchanged)")
        else:
            lts_entry = {"date": "lts", "kernel": lts_value}
            entries.append(lts_entry)
            print(f"-> {lts_value} (added to entries)")
        lts_entry.pop("revision", None)
    else:
        # 其他版本使用根节点 lts
        old_lts = data.get("lts")
        if old_lts != lts_value:
            data["lts"] = lts_value
            print(f"-> {lts_value} (was {old_lts})")
        else:
            print(f"-> {lts_value} (unchanged)")

    data["entries"] = entries
    validate_data(data, path, android_ver, kernel_ver)
    changed = data != original_data
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(temp_path, path)
        print(f"  => Saved {len(entries)} entries to {path}")
    else:
        print(f"  => No changes")

    return changed


def main():
    any_changed = False
    failures: list[tuple[str, str, BaseException]] = []
    for (android_ver, kernel_ver), (date_start, date_end, dep_cutoff) in TARGETS.items():
        print(f"\n=== {android_ver} / {kernel_ver} ===")
        try:
            if update_target(android_ver, kernel_ver, date_start, date_end, dep_cutoff):
                any_changed = True
        except Exception as error:
            # 每个 (android, kernel) 的数据文件互相独立，一个目标的异常没有理由
            # 让其它目标已经算好的更新一起丢掉。这里先记下来跑完全部目标，最后
            # 再统一以非零状态退出。
            failures.append((android_ver, kernel_ver, error))
            print(f"  !! {android_ver}/{kernel_ver} failed: {error}", file=sys.stderr)

    print(f"\n{'Data updated.' if any_changed else 'All data up-to-date.'}")
    if failures:
        detail = "; ".join(f"{a}/{k}: {e}" for a, k, e in failures)
        raise RuntimeError(f"{len(failures)} target(s) failed: {detail}")
    return any_changed


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
