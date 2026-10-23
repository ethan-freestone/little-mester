import { definePlugin } from "@opencode-ai/plugin/tui"

export default definePlugin({
  id: "little-mester-logo",
  name: "Little Mester Logo",
  description: "Replace the default logo with Little Mester ASCII art",
  version: "1.0.0",
  
  setup: async ({ api }) => {
    const fs = await import("fs/promises")
    const path = await import("path")
    
    try {
      const logoPath = path.join(__dirname, "little-mester-logo.txt")
      const logo = await fs.readFile(logoPath, "utf-8")
      
      if (api.theme && api.theme.setLogo) {
        api.theme.setLogo(logo)
      }
    } catch (error) {
      console.error("Failed to load Little Mester logo:", error)
    }
  }
})