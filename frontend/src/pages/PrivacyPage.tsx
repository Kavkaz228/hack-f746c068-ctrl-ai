import { Link } from 'react-router-dom'
import { PRIVACY_SECTIONS } from '../legal/texts'
import { Card } from '../components/ui'

export function PrivacyPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <Link to="/" className="inline-flex rounded-full bg-white/60 px-4 py-1.5 text-sm font-medium text-slate-600 ring-1 ring-black/5 hover:bg-white">← Career Quest</Link>
      <Card className="mt-6 !p-8">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">Политика обработки персональных данных</h1>
        <p className="mt-2 text-sm text-slate-500">Как Career Quest собирает, хранит и защищает данные сотрудников.</p>
        <div className="mt-6 space-y-5 text-[15px] leading-relaxed text-slate-700">
          {PRIVACY_SECTIONS.map((section) => (
            <section key={section.title}>
              <h2 className="text-lg font-semibold text-slate-900">{section.title}</h2>
              {section.items.map((item) => <p key={item} className="mt-1">{item}</p>)}
            </section>
          ))}
        </div>
      </Card>
    </div>
  )
}
