import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// jsdom does not implement these
Element.prototype.scrollIntoView = function scrollIntoView() {}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
