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
        # sem isso, o log4j (sem provider configurado) escreve um aviso na
        # stdout, poluindo a saida JSON deste script
        jpype.startJVM("-Dlog4j2.StatusLogger.level=OFF")
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

    final_date = parse_date_arg(args.final) if args.final else None
    final_ldt = to_java_ldt(jpype, final_date.replace(hour=23, minute=59)) if final_date else None

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

        # sem prazo final: usa o passe simples (duracao restante = duracao total x % pendente)
        bloqueada = False
        comprimida = False
        if final_ldt is None:
            duration = t.getDuration()
            if duration is None:
                continue
            remaining = Duration.getInstance(duration.getDuration() * (1.0 - pct / 100.0), duration.getUnits())
            cal = calendar_for(t)
            new_finish = cal.getDate(new_start, remaining)
        else:
            # com prazo final: e um limite rigido — o restante da tarefa e
            # comprimido para caber entre o inicio possivel e o prazo final,
            # nunca empurrando a data para frente. Tarefas cuja propria
            # predecessora ja termina depois do prazo ficam marcadas como
            # bloqueadas (nao ha como caber so ajustando a duracao).
            if new_start.isAfter(final_ldt):
                bloqueada = True
                new_start = final_ldt
            duration = t.getDuration()
            if duration is None:
                continue
            cal = calendar_for(t)
            naive_remaining = Duration.getInstance(duration.getDuration() * (1.0 - pct / 100.0), duration.getUnits())
            naive_finish = cal.getDate(new_start, naive_remaining)
            if naive_finish.isAfter(final_ldt):
                comprimida = True
                new_finish = final_ldt
            else:
                new_finish = naive_finish

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
            "comprimida": comprimida,
            "bloqueada": bloqueada,
        })

    all_finishes = [to_pydate(t.getFinish()) for t in tasks if t.getFinish() is not None]
    novo_termino_projeto = max(all_finishes) if all_finishes else None

    report = render_report(args, cutoff, before, updates, replan, novo_termino_projeto, final_date)
    with open(args.out_report, "w", encoding="utf-8") as f:
        f.write(report)

    if args.out_project:
        from org.mpxj.mspdi import MSPDIWriter
        MSPDIWriter().write(project, args.out_project)

    if args.out_gantt:
        flags_by_id = {r["id"]: r for r in replan}
        chart_rows = []
        for t in tasks:
            uid = int(t.getUniqueID())
            start = to_pydate(t.getStart())
            finish = to_pydate(t.getFinish())
            if start is None or finish is None:
                continue
            pct = to_pct(t.getPercentageComplete())
            flag = flags_by_id.get(uid, {})
            chart_rows.append({
                "wbs": task_wbs(t),
                "nome": str(t.getName()),
                "inicio": start,
                "termino": finish,
                "percentual": pct,
                "comprimida": flag.get("comprimida", False),
                "bloqueada": flag.get("bloqueada", False),
            })
        render_gantt_pdf(chart_rows, cutoff, final_date, args.out_gantt)

    print(json.dumps({
        "relatorio": args.out_report,
        "projeto_atualizado": args.out_project,
        "gantt_pdf": args.out_gantt,
        "novo_termino_projeto": novo_termino_projeto.strftime("%Y-%m-%d") if novo_termino_projeto else None,
        "data_final_alvo": args.final,
        "tarefas_replanejadas": len(replan),
        "tarefas_comprimidas": sum(1 for r in replan if r.get("comprimida") and not r.get("bloqueada")),
        "tarefas_bloqueadas": sum(1 for r in replan if r.get("bloqueada")),
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
        lines.append("| Tarefa | % | Termino antigo | Termino novo | |")
        lines.append("|---|---|---|---|---|")
        for r in replan:
            antigo = r["termino_antigo"].strftime("%d/%m/%Y") if r["termino_antigo"] else "-"
            novo = r["termino_novo"].strftime("%d/%m/%Y") if r["termino_novo"] else "-"
            marca = "🔴 bloqueada" if r.get("bloqueada") else ("🟡 comprimida" if r.get("comprimida") else "")
            lines.append(f"| {r['nome']} | {r['percentual']:.0f}% | {antigo} | {novo} | {marca} |")
    else:
        lines.append("Nenhuma tarefa precisou ser replanejada.")
    lines.append("")

    lines.append("## Data final do projeto")
    lines.append("")
    if novo_termino_projeto:
        lines.append(f"- Novo termino previsto (apos replanejamento): **{novo_termino_projeto.strftime('%d/%m/%Y')}**")
    if final_date:
        lines.append(f"- Data final alvo (limite rigido): **{final_date.strftime('%d/%m/%Y')}**")
        if novo_termino_projeto:
            delta = (novo_termino_projeto.date() - final_date.date()).days
            if delta > 0:
                lines.append(f"- ⚠️ Mesmo comprimindo o restante das tarefas, o projeto ainda passaria "
                              f"**{delta} dia(s)** do prazo (ver tarefas bloqueadas abaixo).")
            else:
                lines.append(f"- Cronograma comprimido para caber ate a data final "
                              f"(folga de {-delta} dia(s)).")
    lines.append("")

    comprimidas = [r for r in replan if r.get("comprimida") and not r.get("bloqueada")]
    bloqueadas = [r for r in replan if r.get("bloqueada")]
    if comprimidas or bloqueadas:
        lines.append("## Pontos de atencao (tarefas comprimidas para caber no prazo)")
        lines.append("")
        if bloqueadas:
            lines.append("**🔴 Bloqueadas** — a propria predecessora so termina depois do prazo final; "
                          "nao ha como encaixar so ajustando a duracao desta tarefa, precisa agir na "
                          "predecessora ou liberar essa dependencia:")
            for r in bloqueadas:
                lines.append(f"- {r['nome']} ({r['percentual']:.0f}% concluido)")
            lines.append("")
        if comprimidas:
            lines.append("**🟡 Comprimidas** — o ritmo normal (duracao total x % pendente) terminaria "
                          "depois do prazo; para caber ate a data final, o restante desta tarefa foi "
                          "encaixado num prazo menor do que o ritmo atual sugere — considere reforcar "
                          "equipe/turno nelas:")
            for r in comprimidas:
                lines.append(f"- {r['nome']} ({r['percentual']:.0f}% concluido)")
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


def render_gantt_pdf(rows, cutoff, final_date, out_path, tasks_per_page=40):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_pdf import PdfPages

    window_start = cutoff.date() - datetime.timedelta(days=2)
    window_end = (final_date or cutoff).date() + datetime.timedelta(days=2)
    for r in rows:
        window_end = max(window_end, r["termino"].date())
    window_end += datetime.timedelta(days=2)

    with PdfPages(out_path) as pdf:
        n_pages = max(1, (len(rows) + tasks_per_page - 1) // tasks_per_page)
        for page in range(n_pages):
            chunk_rows = rows[page * tasks_per_page:(page + 1) * tasks_per_page]
            fig_h = max(2.5, 0.32 * len(chunk_rows) + 1.2)
            fig, ax = plt.subplots(figsize=(14, fig_h))

            ylabels = []
            for i, r in enumerate(chunk_rows):
                y = len(chunk_rows) - i
                start = r["inicio"].date()
                finish = r["termino"].date()
                clipped_start = max(start, window_start)
                clipped_finish = max(min(finish, window_end), clipped_start)
                total_days = max((clipped_finish - clipped_start).days, 0.3)
                done_days = total_days * (r["percentual"] / 100.0)

                if r["bloqueada"]:
                    edge, face_done, face_rem = "#b3261e", "#b3261e", "#f2c9c6"
                elif r["comprimida"]:
                    edge, face_done, face_rem = "#8a6d00", "#c9a227", "#f2e3b3"
                elif r["percentual"] >= 100:
                    edge, face_done, face_rem = "#2e7d32", "#66bb6a", "#66bb6a"
                else:
                    edge, face_done, face_rem = "#1f4e79", "#5b8ec4", "#cfe0f0"

                ax.barh(y, total_days, left=mdates.date2num(clipped_start),
                        color=face_rem, edgecolor=edge, height=0.55, linewidth=0.8)
                if done_days > 0:
                    ax.barh(y, done_days, left=mdates.date2num(clipped_start),
                            color=face_done, edgecolor=edge, height=0.55, linewidth=0.8)

                label = f"{r['wbs']}  {r['nome'][:55]}"
                if start < window_start:
                    label += f"  (iniciou {start.strftime('%d/%m')})"
                ylabels.append(f"{label}  [{r['percentual']:.0f}%]")

            ax.set_yticks(range(len(chunk_rows), 0, -1))
            ax.set_yticklabels(ylabels, fontsize=6.5)
            ax.set_xlim(mdates.date2num(window_start), mdates.date2num(window_end))
            ax.xaxis_date()
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, (window_end - window_start).days // 20)))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7)

            ax.axvline(mdates.date2num(cutoff.date()), color="#444", linestyle="--", linewidth=1)
            ax.text(mdates.date2num(cutoff.date()), len(chunk_rows) + 0.6, "hoje",
                    fontsize=7, color="#444", ha="center")
            if final_date:
                ax.axvline(mdates.date2num(final_date.date()), color="#b3261e", linestyle="--", linewidth=1)
                ax.text(mdates.date2num(final_date.date()), len(chunk_rows) + 0.6, "prazo final",
                        fontsize=7, color="#b3261e", ha="center")

            ax.set_ylim(0.3, len(chunk_rows) + 1.2)
            ax.grid(axis="x", linestyle=":", linewidth=0.5, alpha=0.6)
            ax.set_title(f"Cronograma — pagina {page + 1}/{n_pages}", fontsize=10, loc="left")
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)


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
    p_apply.add_argument("--out-gantt", default=None, help="Caminho opcional para um grafico de Gantt em PDF (requer matplotlib)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
