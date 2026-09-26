import { STEP_LABELS, type StepView } from '../app/derive';

interface Props {
  view: StepView;
}

/** 四步条：由真实执行阶段点亮，不是装饰 */
export function StepBar({ view }: Props) {
  const labels: string[] = [...STEP_LABELS];
  labels[3] = view.step4Label;
  return (
    <ol className="stepbar" aria-label="运行阶段">
      {labels.map((label, i) => {
        const n = i + 1;
        const cls = [
          'step',
          n === view.index ? 'is-active' : '',
          n < view.index ? 'is-done' : '',
          n === 4 && view.step4Pending ? 'is-pending' : '',
        ].filter(Boolean).join(' ');
        return (
          <li key={label} className={cls}>
            <span className="step__no">第 {n} 步</span>
            <span className="step__label">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}
