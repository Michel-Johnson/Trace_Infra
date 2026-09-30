import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { WorkspaceIcon as Icon } from './WorkspaceIcon';
import './WorkspaceSelect.css';

export type WorkspaceSelectOption<T extends string> = { value: T; label: string };

export function WorkspaceSelect<T extends string>({ value, options, onChange, disabled = false, ariaLabel, className = '' }: {
  value: T;
  options: Array<WorkspaceSelectOption<T>>;
  onChange: (value: T) => void;
  disabled?: boolean;
  ariaLabel: string;
  className?: string;
}) {
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selectedIndex = Math.max(0, options.findIndex(option => option.value === value));
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  useEffect(() => { setActiveIndex(selectedIndex); }, [selectedIndex]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  const choose = (index: number) => { const option = options[index]; if (!option) return; onChange(option.value); setActiveIndex(index); setOpen(false); };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled || !options.length) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); setOpen(true);
      setActiveIndex(index => event.key === 'ArrowDown' ? (index + 1) % options.length : (index - 1 + options.length) % options.length);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); if (open) choose(activeIndex); else setOpen(true);
    } else if (event.key === 'Escape') { setOpen(false); }
  };
  return <div ref={root} className={`th-workspace-select ${open ? 'open' : ''} ${disabled ? 'disabled' : ''} ${className}`}>
    <button type="button" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} disabled={disabled} onClick={() => setOpen(value => !value)} onKeyDown={onKeyDown}>
      <span>{options[selectedIndex]?.label || value}</span><Icon name="chevron" size={15}/>
    </button>
    {open && <div id={listId} className="th-workspace-select-menu" role="listbox" aria-label={ariaLabel}>{options.map((option,index) => <button type="button" role="option" aria-selected={option.value === value} className={`${option.value === value ? 'selected' : ''} ${index === activeIndex ? 'active' : ''}`} key={option.value} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(index)}><span>{option.label}</span>{option.value === value && <Icon name="check" size={16}/>}</button>)}</div>}
  </div>;
}
