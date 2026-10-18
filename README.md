# Jarvis pessoal V0.1

Assistente por voz para Windows, independente da Enfoque e de qualquer servidor corporativo. O reconhecimento de fala e a IA funcionam localmente; a voz neural pt-BR-AntonioNeural usa o servico gratuito do Microsoft Edge TTS e precisa de internet.

## Instalar e iniciar

No desktop, abra a pasta Jarvis na Area de Trabalho e execute **Instalar Jarvis.bat**. Ou pelo PowerShell: `powershell -ExecutionPolicy Bypass -File .\install.ps1`.

O instalador cria um ambiente isolado de Python, baixa Vosk portugues, instala o Ollama, baixa o modelo Qwen2.5:1.5b, valida a inicializacao e registra o app para iniciar com Windows. Depois use **Iniciar Jarvis.bat**.

## Como usar

- Jarvis escuta sempre pelo microfone em segundo plano, inclusive enquanto voce joga.
- Diga **"Jarvis desligar"**, **"desliga Jarvis"** ou **"hora de dormir"** para silenciar as respostas, mas manter a escuta local.
- Quando estiver silenciado, diga **"Jarvis ligar"** ou inclua Jarvis em qualquer posicao da pergunta para reativar e responder.
- Fechar o painel o esconde; para encerrar o processo de verdade, use o icone do Jarvis ao lado do relogio e clique **Sair do Jarvis**.
- Se nao quiser falar, digite no campo inferior e tecle Enter.

**Limites:** esta V0.1 conversa, mas ainda nao controla arquivos, teclado nem mouse. O modelo e pequeno para rodar em CPUs antigas e pode levar segundos para responder. A voz neural depende da internet. Nao existem respostas falsas de fallback quando o modelo falha.

## Arquivos

- `jarvis.py`: interface, bandeja Windows, microfone, Vosk, TTS e modelo local.
- `install.ps1`: instalador e validacao.
- `requirements.txt`: dependencias.
- `models/` e `.venv/`: baixados localmente e ignorados pelo Git.

**Diagnostico:** `.\.venv\Scripts\python.exe .\jarvis.py --check`. Logs em `%LOCALAPPDATA%\Jarvis\jarvis.log`.

O repositorio e pessoal. Nao publica modelos locais, dados de microfone, chaves, logs nem conversas.
