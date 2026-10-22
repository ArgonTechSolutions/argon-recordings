import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AuthGate, AdminPanel } from "@/components/accounts"
import { ThemeProvider } from "@/components/theme-provider"
import "./index.css"
const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={cache}>
        <AuthGate audience="admin">
          <AdminPanel />
        </AuthGate>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>
)
