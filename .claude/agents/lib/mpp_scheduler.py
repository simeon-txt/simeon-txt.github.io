#!/usr/bin/env python3
"""Ferramenta de apoio ao subagente 'gestor-cronograma'.

Le arquivos de cronograma (.mpp, .mpx, MSPDI .xml, Primavera, GanttProject,
Planner, etc. -- qualquer formato suportado pela biblioteca MPXJ) e:

  inspect  -> lista as tarefas que deveriam estar em andamento ou concluidas
              ate uma data de corte (atrasadas + do dia), para o subagente
              perguntar ao usuario o percentual real de cada uma.

  apply    -> aplica os percentuais informados (updates.json), reprograma
              para frente as tarefas ainda nao concluidas (passe simples de
              propagacao respeitando predecessoras Termino->Inicio e o
              calendario de cada tarefa) e gera:
                - um relatorio em markdown (progresso do dia + plano dos
                  proximos dias ate a data final)
                - opcionalmente um novo arquivo de projeto (MSPDI .xml),
                  que pode ser reaberto no MS Project.

Limitacoes conhecidas (documentadas para quem revisar o relatorio):
  - So o tipo de dependencia Termino->Inicio (Finish-to-Start) e propagado
    com precisao; os demais tipos (SS/FF/SF) usam uma aproximacao simples.
  - Nao ha nivelamento de recursos (resource leveling): a reprogramacao so
    olha para datas e dependencias, nao para disponibilidade de recursos.
  - Duracao restante = duracao original x (1 - percentual concluido).

Dependencias (instale antes de usar):
    pip install -r requirements.txt   # JPype1, mpxj
"""
import argparse
import datetime
import json
import sys


def ensure_jvm():
    import jpype
    import mpxj  # noqa: F401  (efeito colateral: registra os jars da MPXJ no classpath)
    if not jpype.isJVMStarted():
        jpype.startJVM()
    return jpype


def read_project(jpype, path):
    from org.mpxj.reader import UniversalProjectReader
    reader = UniversalProjectReader()
    project = reader.read(path)
    if project is None:
        raise RuntimeError(f"Nao foi possivel identificar/ler o formato do arquivo: {path}")
    return project


def flatten_tasks(project):
    result = []

    def walk(tasks):
        for t in tasks:
            result.append(t)
            children = t.getChildTasks()
            if children is not None and children.size() > 0:
                walk(children)

    walk(project.getChildTasks())
    return result


def to_pydate(ldt):
    if ldt is None:
        return None
    return datetime.datetime(ldt.getYear(), ldt.getMonthValue(), ldt.getDayOfMonth(),
                              ldt.getHour(), ldt.getMinute())


def to_java_ldt(jpype, py_dt):
    from java.time import LocalDateTime
    return LocalDateTime.of(py_dt.year, py_dt.month, py_dt.day,
                             getattr(py_dt, "hour", 0), getattr(py_dt, "minute", 0))


def to_pct(number):
    if number is None:
        return 0.0
    return float(number)


def task_wbs(t):
    on = t.getOutlineNumber()
    return str(on) if on is not None else ""


def parse_date_arg(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d")


def cmd_inspect(args):
    jpype = ensure_jvm()
    project = read_project(jpype, args.file)
    cutoff = parse_date_arg(args.cutoff)
    cutoff_end = cutoff.replace(hour=23, minute=59)

    items = []
    for t in flatten_tasks(project):
        if t.getSummary():
            continue
        start = to_pydate(t.getStart())
        finish = to_pydate(t.getFinish())
        if start is None or finish is None:
            continue
        pct = to_pct(t.getPercentageComplete())
        if pct >= 100:
            continue

        status = None
        if finish < cutoff:
            status = "atrasada"
        elif start <= cutoff_end <= finish:
            status = "prevista_para_hoje"
        if status is None:
            continue

        items.append({
            "id": int(t.getUniqueID()),
            "wbs": task_wbs(t),
            "nome": str(t.getName()),
            "inicio": start.strftime("%Y-%m-%d"),
            "termino_previsto": finish.strftime("%Y-%m-%d"),
            "percentual_atual": pct,
            "status": status,
            "dias_atraso": max(0, (cutoff.date() - finish.date()).days),
        })

    items.sort(key=lambda x: (x["status"] != "atrasada", x["termino_previsto"]))
    print(json.dumps({
        "arquivo": args.file,
        "data_corte": args.cutoff,
        "itens_para_perguntar": items,
        "total": len(items),
    }, ensure_ascii=False, indent=2))


def build_predecessor_map(tasks):
    from org.mpxj import RelationType
    pred_map = {}
    for t in tasks:
        preds = t.getPredecessors()
        entries = []
        if preds is not None:
            for r in preds:
                entries.append((r.getPredecessorTask(), str(r.getType().name()), r.getLag()))
        pred_map[int(t.getUniqueID())] = entries
    return pred_map, RelationType


def topo_order(tasks, pred_map):
    ids = [int(t.getUniqueID()) for t in tasks]
    id_to_task = {int(t.getUniqueID()): t for t in tasks}
    indeg = {i: 0 for i in ids}
    children = {i: [] for i in ids}
    for i in ids:
        for pred_task, _rtype, _lag in pred_map.get(i, []):
            pid = int(pred_task.getUniqueID())
            if pid in indeg:
                indeg[i] += 1
                children[pid].append(i)

    queue = [i for i in ids if indeg[i] == 0]
    order = []
    seen = set()
    while queue:
        i = queue.pop(0)
        if i in seen:
            continue
        seen.add(i)
        order.append(i)
        for c in children[i]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    # tarefas que sobraram (ciclo, ou nao alcancadas) entram no fim na ordem original
    for i in ids:
        if i not in seen:
            order.append(i)
    return [id_to_task[i] for i in order]


def cmd_apply(args):
    jpype = ensure_jvm()
    from org.mpxj import Duration, TimeUnit, RelationType

    project = read_project(jpype, args.file)
    cutoff = parse_date_arg(args.cutoff)
    cutoff_ldt = to_java_ldt(jpype, cutoff)

    with open(args.updates, encoding="utf-8") as f:
        updates = json.load(f)
    updates = {int(k): float(v) for k, v in updates.items()}

    tasks = [t for t in flatten_tasks(project) if not t.getSummary()]
    pred_map, _ = build_predecessor_map(tasks)

    before = {}
    for t in tasks:
        uid = int(t.getUniqueID())
        before[uid] = {
            "nome": str(t.getName()),
            "pct_antes": to_pct(t.getPercentageComplete()),
            "termino_antes": to_pydate(t.getFinish()),
        }
        if uid in updates:
            new_pct = max(0.0, min(100.0, updates[uid]))
            t.setPercentageComplete(java_number(jpype, new_pct))
            if new_pct >= 100 and t.getActualFinish() is None:
                t.setActualFinish(cutoff_ldt)
            if new_pct > 0 and t.getActualStart() is None:
                t.setActualStart(t.getStart() if t.getStart() is not None else cutoff_ldt)

    ordered = topo_order(tasks, pred_map)

    def calendar_for(t):
        cal = t.getEffectiveCalendar()
        if cal is None:
            cal = project.getDefaultCalendar()
        return cal

    replan = []
    for t in ordered:
        uid = int(t.getUniqueID())
        pct = to_pct(t.getPercentageComplete())
        if pct >= 100:
            continue

        preds = pred_map.get(uid, [])
        earliest = cutoff_ldt
        for pred_task, rtype, lag in preds:
            pf = pred_task.getFinish()
            ps = pred_task.getStart()
            if rtype == "FINISH_START" and pf is not None:
                anchor = pf
            elif rtype in ("START_START", "START_FINISH") and ps is not None:
                anchor = ps
            elif pf is not None:
                anchor = pf
            else:
                continue
            if lag is not None and lag.getDuration() != 0:
                cal = calendar_for(t)
                anchor = cal.getDate(anchor, lag)
            if anchor is not None and anchor.isAfter(earliest):
                earliest = anchor

        cur_start = t.getStart()
        new_start = earliest if (cur_start is None or earliest.isAfter(cur_start) or pct == 0) else cur_start
        if not new_start.isAfter(cutoff_ldt) and pct > 0:
            new_start = cutoff_ldt if cutoff_ldt.isAfter(new_start) else new_start

        duration = t.getDuration()
        if duration is None:
            continue
        remaining = Duration.getInstance(duration.getDuration() * (1.0 - pct / 100.0), duration.getUnits())

        cal = calendar_for(t)
        new_finish = cal.getDate(new_start, remaining)

        old_start = to_pydate(t.getStart())
        old_finish = to_pydate(t.getFinish())
        t.setStart(new_start)
        t.setFinish(new_finish)

        dias_ativos = []
        day = to_pydate(new_start).date()
        end_day = to_pydate(new_finish).date()
        while day <= end_day:
            from java.time import LocalDate
            jday = LocalDate.of(day.year, day.month, day.day)
            if cal.isWorkingDate(jday):
                dias_ativos.append(day)
            day += datetime.timedelta(days=1)

        replan.append({
            "id": uid,
            "nome": str(t.getName()),
            "percentual": pct,
            "inicio_antigo": old_start,
            "termino_antigo": old_finish,
            "inicio_novo": to_pydate(new_start),
            "termino_novo": to_pydate(new_finish),
            "dias_ativos": dias_ativos,
        })

    all_finishes = [to_pydate(t.getFinish()) for t in tasks if t.getFinish() is not None]
    novo_termino_projeto = max(all_finishes) if all_finishes else None

    final_date = parse_date_arg(args.final) if args.final else None

    report = render_report(args, cutoff, before, updates, replan, novo_termino_projeto, final_date)
    with open(args.out_report, "w", encoding="utf-8") as f:
        f.write(report)

    if args.out_project:
        from org.mpxj.mspdi import MSPDIWriter
        MSPDIWriter().write(project, args.out_project)

    print(json.dumps({
        "relatorio": args.out_report,
        "projeto_atualizado": args.out_project,
        "novo_termino_projeto": novo_termino_projeto.strftime("%Y-%m-%d") if novo_termino_projeto else None,
        "data_final_alvo": args.final,
        "tarefas_replanejadas": len(replan),
    }, ensure_ascii=False, indent=2))


def java_number(jpype, value):
    from java.lang import Double
    return Double(value)


def render_report(args, cutoff, before, updates, replan, novo_termino_projeto, final_date):
    lines = []
    lines.append(f"# Relatorio de avanco - {cutoff.strftime('%d/%m/%Y')}")
    lines.append("")
    lines.append(f"Arquivo de origem: `{args.file}`")
    lines.append("")
    lines.append("## Avanco informado hoje")
    lines.append("")
    lines.append("| Tarefa | % antes | % informado hoje |")
    lines.append("|---|---|---|")
    for uid, pct in sorted(updates.items(), key=lambda kv: kv[0]):
        b = before.get(uid, {})
        lines.append(f"| {b.get('nome', uid)} | {b.get('pct_antes', 0):.0f}% | {pct:.0f}% |")
    lines.append("")

    lines.append("## Tarefas replanejadas (ainda nao concluidas)")
    lines.append("")
    if replan:
        lines.append("| Tarefa | % | Termino antigo | Termino novo |")
        lines.append("|---|---|---|---|")
        for r in replan:
            antigo = r["termino_antigo"].strftime("%d/%m/%Y") if r["termino_antigo"] else "-"
            novo = r["termino_novo"].strftime("%d/%m/%Y") if r["termino_novo"] else "-"
            lines.append(f"| {r['nome']} | {r['percentual']:.0f}% | {antigo} | {novo} |")
    else:
        lines.append("Nenhuma tarefa precisou ser replanejada.")
    lines.append("")

    lines.append("## Data final do projeto")
    lines.append("")
    if novo_termino_projeto:
        lines.append(f"- Novo termino previsto (apos replanejamento): **{novo_termino_projeto.strftime('%d/%m/%Y')}**")
    if final_date:
        lines.append(f"- Data final alvo: **{final_date.strftime('%d/%m/%Y')}**")
        if novo_termino_projeto:
            delta = (novo_termino_projeto.date() - final_date.date()).days
            if delta > 0:
                lines.append(f"- ⚠️ Projeto tende a atrasar **{delta} dia(s)** em relacao a data final, "
                              f"mantido o ritmo atual.")
            else:
                lines.append(f"- Projeto dentro do prazo (folga de {-delta} dia(s)).")
    lines.append("")

    lines.append("## Plano dos proximos dias")
    lines.append("")
    horizon_end = final_date or novo_termino_projeto
    if horizon_end and replan:
        day = cutoff.date()
        end = horizon_end.date()
        while day <= end:
            active = [r for r in replan if day in r.get("dias_ativos", [])]
            if active:
                lines.append(f"**{day.strftime('%d/%m/%Y')}**")
                for r in active:
                    lines.append(f"- {r['nome']} ({r['percentual']:.0f}% concluido)")
                lines.append("")
            day += datetime.timedelta(days=1)
    else:
        lines.append("Sem tarefas pendentes para planejar.")

    lines.append("")
    lines.append("---")
    lines.append("_Observacoes: reprogramacao simplificada (dependencias Termino->Inicio, "
                  "sem nivelamento de recursos). Revise no MS Project antes de comunicar prazos "
                  "criticos._")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="Lista tarefas atrasadas/do dia para perguntar ao usuario")
    p_inspect.add_argument("file")
    p_inspect.add_argument("--cutoff", required=True, help="Data de corte YYYY-MM-DD (normalmente hoje)")
    p_inspect.set_defaults(func=cmd_inspect)

    p_apply = sub.add_parser("apply", help="Aplica percentuais, reprograma e gera relatorio")
    p_apply.add_argument("file")
    p_apply.add_argument("--cutoff", required=True, help="Data de corte YYYY-MM-DD (normalmente hoje)")
    p_apply.add_argument("--updates", required=True, help="JSON {task_id: percentual}")
    p_apply.add_argument("--out-report", required=True, help="Caminho do relatorio .md de saida")
    p_apply.add_argument("--out-project", default=None, help="Caminho opcional para salvar o projeto atualizado (.xml MSPDI)")
    p_apply.add_argument("--final", default=None, help="Data final alvo do projeto YYYY-MM-DD (opcional)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
