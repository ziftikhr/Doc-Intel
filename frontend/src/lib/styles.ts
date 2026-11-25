// Small styling helpers shared by components (kept out of component files so
// React Fast Refresh can hot-reload those).

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

export const inputClass =
  'block w-full rounded-lg border-0 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500'
