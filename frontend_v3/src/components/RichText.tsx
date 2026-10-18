import type { ReactNode } from 'react';

const INLINE = /(\*\*[^*\n]+\*\*|\[[^\]\n]{1,80}\]|`[^`\n]+`)/g;

/**
 * 极简行内渲染：只认 **加粗**、`代码` 和 [chunk id] 引用标记。
 * 不做完整 Markdown 解析（不需要，也不引第三方库）。
 */
export function RichText({ text, onCite }: { text: string; onCite?: (raw: string) => void }) {
  const parts = text.split(INLINE).filter((s) => s !== '');
  return (
    <>
      {parts.map((p, i) => {
        const key = `${i}-${p.slice(0, 8)}`;
        if (p.startsWith('**') && p.endsWith('**')) return <strong key={key}>{p.slice(2, -2)}</strong>;
        if (p.startsWith('`') && p.endsWith('`')) return <code key={key}>{p.slice(1, -1)}</code>;
        if (p.startsWith('[') && p.endsWith(']')) {
          // v1.3 对话流：对话页传入 onCite 时引用可点开引用抽屉；批量页等不传则保持纯展示
          return (
            <span
              key={key}
              className={onCite ? 'cite cite--link' : 'cite'}
              title={onCite ? '查看引用详情' : undefined}
              onClick={onCite ? () => onCite(p) : undefined}
            >
              {p}
            </span>
          );
        }
        return <span key={key}>{p}</span>;
      })}
    </>
  );
}

/** 按空行分段，段内用 RichText */
export function RichBody({ text, onCite }: { text: string; onCite?: (raw: string) => void }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <>
      {blocks.map((b, i) => (
        <p className="rich-p" key={`${i}-${b.slice(0, 6)}`}>
          <RichText text={b} onCite={onCite} />
        </p>
      ))}
    </>
  );
}

export type { ReactNode };
