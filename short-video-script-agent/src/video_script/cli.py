import argparse
import json
from pathlib import Path
import sys

from .agent import run_agent
from .domain import Request
from .models import LazyChatModel,OfflineMock
from .tools import FAULTS


def write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def markdown(result):
    script=result.get("script")
    if not script:
        return "# 本次没有通过检查的可交付脚本\n\n状态："+result["task_status"]+"\n\n请查看输出 JSON 中的缺口、未批准草稿及检查结果。\n"
    lines=["# "+script["title"],"",f"平台：{script['brief']['platform']}；受众：{script['brief']['audience']}；风格：{script['brief']['style']}",
           f"预计总时长：{script['estimated_total_seconds']} 秒（确定性估算，非实测 TTS）","",
           "| 分镜 | 预计秒数 | 画面 | 旁白 / 字幕 | 来源 |","|---|---:|---|---|---|"]
    times={r["scene_id"]:r["estimated_seconds"] for r in script["scene_durations"]}
    for s in script["scenes"]:
        source=(s["source_id"]+" / "+s["fact_id"]) if s["speech_id"]=="FACT" else "已审查创作表达"
        lines.append(f"| {s['scene_id']} | {times[s['scene_id']]} | {s['visual']} | {s['narration']} | {source} |")
    lines.extend(["","## 产品事实来源",""])
    lines.extend(f"- {c['source_id']} / {c['fact_id']} / {c['version_date']}：{c['quote']}" for c in script["citations"])
    lines.extend(["","可编辑文件；修改后需重新检查，库外自由改写超出本原型的自动安全保证范围。",""])
    return "\n".join(lines)


def export_result(output,result):
    output=Path(output)
    write_json(output,result)
    write_json(output.with_name(output.stem+".script.json"),result["script"] or {"task_status":result["task_status"],"script":None,"risks":result["risks"]})
    output.with_name(output.stem+".script.md").write_text(markdown(result),encoding="utf-8")


def main(argv=None):
    if hasattr(sys.stdout,"reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="Source-bound editable short-video script agent")
    parser.add_argument("--input",required=True)
    parser.add_argument("--mode",choices=["mock","live"],default="mock")
    parser.add_argument("--data-dir",default="data")
    parser.add_argument("--output",default=".runtime/output.json")
    parser.add_argument("--trace",default=".runtime/trace.json")
    parser.add_argument("--max-steps",type=int,default=18)
    parser.add_argument("--fault",choices=FAULTS,action="append",default=[])
    args=parser.parse_args(argv)
    try:
        request=Request.parse(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
        model=OfflineMock() if args.mode=="mock" else LazyChatModel()
        result,trace=run_agent(request,args.data_dir,model,max_steps=args.max_steps,faults=args.fault)
        export_result(args.output,result)
        write_json(args.trace,trace)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result["task_status"]=="SCRIPT_READY" else 2
    except (ValueError,TypeError,OSError,KeyError) as exc:
        print(json.dumps({"task_status":"INPUT_OR_CONFIG_ERROR","error_type":type(exc).__name__}),file=sys.stderr)
        return 2


if __name__=="__main__": raise SystemExit(main())
