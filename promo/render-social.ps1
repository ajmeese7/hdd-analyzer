# Re-render promo/social.png from promo/social.svg at 2x (2560x1280).
# resvg ignores the @font-face embedded in the SVG, so the Consolas files are
# passed explicitly; the embedded copy is only for browsers and GitHub's preview.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
npx -y @resvg/resvg-js-cli@2.6.2-beta.1 `
  --text-rendering 1 `
  --fit-width 2560 `
  --font-file "$env:WINDIR\Fonts\consola.ttf" `
  --font-file "$env:WINDIR\Fonts\consolab.ttf" `
  social.svg social.png
