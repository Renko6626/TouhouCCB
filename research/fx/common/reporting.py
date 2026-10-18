"""Plain Markdown tables from already-computed results."""


def table(rows, columns):
    out = ['| '+' | '.join(label for _,label,_ in columns)+' |',
           '| '+' | '.join('---' for _ in columns)+' |']
    for row in rows:
        values = []
        for key, _, fmt in columns:
            value = row.get(key)
            values.append('不适用' if value is None else (fmt.format(value) if fmt else str(value)))
        out.append('| '+' | '.join(values)+' |')
    return '\n'.join(out)
