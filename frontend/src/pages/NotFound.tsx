import { Link } from 'react-router'
import { Empty } from '../components/ui'

export function NotFound() {
  return (
    <Empty title="Page not found">
      <Link to="/" className="text-indigo-600 hover:underline">Back to the dashboard</Link>
    </Empty>
  )
}
