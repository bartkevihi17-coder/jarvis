# Jarvis pessoal V0.4

Assistente por voz para Windows, independente da Enfoque e de qualquer servidor corporativo. O reconhecimento de fala e a IA funcionam localmente; a voz neural pt-BR-AntonioNeural usa o servico gratuito do Microsoft Edge TTS e precisa de internet.

## Instalar e iniciar

No desktop, abra a pasta Jarvis na Area de Trabalho e execute **Instalar Jarvis.bat**. Ou pelo PowerShell: `powershell -ExecutionPolicy Bypass -File .\install.ps1`.

O instalador cria um ambiente isolado de Python, baixa Whisper em portugues, instala o Ollama, baixa o modelo Qwen2.5:1.5b, valida a inicializacao e registra o app para iniciar com Windows. Depois use **Iniciar Jarvis.bat**.

## Como usar

- Jarvis escuta sempre pelo microfone em segundo plano, inclusive enquanto voce joga.
- Diga **"Jarvis desligar"**, **"desliga Jarvis"** ou **"hora de dormir"** para silenciar as respostas, mas manter a escuta local.
- Quando estiver silenciado, diga **"Jarvis ligar"** ou inclua Jarvis em qualquer posicao da pergunta para reativar e responder.
- Fechar o painel o esconde; para encerrar o processo de verdade, use o icone do Jarvis ao lado do relogio e clique **Sair do Jarvis**.
- Se nao quiser falar, digite no campo inferior e tecle Enter.

**Limites:** esta V0.4 conversa, mas ainda nao controla arquivos, teclado nem mouse. O modelo e pequeno para rodar em CPUs antigas e pode levar segundos para responder. A voz neural depende da internet. Nao existem respostas falsas de fallback quando o modelo falha.


## Painel de audio e transcricoes (V0.2)

- **Microfone:** no painel, abra o campo "MICROFONE | O que Jarvis escuta", selecione uma entrada e o Jarvis reiniciara apenas a captura. "Atualizar dispositivos" recarrega a lista.
- **Saida de voz:** escolha o alto-falante ou fone em "SAIDA DE VOZ | Onde Jarvis fala". O audio MP3 neural e decodificado e direcionado pelo PortAudio, em vez de usar sempre o dispositivo padrao. Use "Testar saida de voz" para ouvir uma frase curta.
- **Transcricoes:** o quadro "TRANSCRICOES / OUVIDO E RESPOSTAS" mostra com horario as frases reconhecidas no microfone, inclusive enquanto Jarvis esta silenciado, e exibe "JARVIS VAI FALAR" com o texto gerado antes da reproducao. A V0.2 exibe apenas a sessao atual (nao grava conversas no Git).
- A selecao fica armazenada somente no PC em \`%LOCALAPPDATA%\Jarvis\audio-settings.json\`; o Git nao envia esse arquivo.
- O painel pode ficar oculto sem encerrar o microfone. Para encerrar, clique no icone do Jarvis perto do relogio e selecione "Sair do Jarvis".

## Arquivos

- `jarvis.py`: interface, bandeja Windows, microfone, Whisper, TTS e modelo local.
- `install.ps1`: instalador e validacao.
- `requirements.txt`: dependencias.
- `models/` e `.venv/`: baixados localmente e ignorados pelo Git.

**Diagnostico:** `.\.venv\Scripts\python.exe .\jarvis.py --check`. Logs em `%LOCALAPPDATA%\Jarvis\jarvis.log`.

O repositorio e pessoal. Nao publica modelos locais, dados de microfone, chaves, logs nem conversas.

## Diagnostico e testes V0.3

A captura agora calibra o nivel de ruido ao iniciar. O reconhecimento usa Whisper-tiny local com palavras-chave de contexto, sem enviar audio para nuvem. O comando de ativacao curto usa uma voz natural pregerada em cache para evitar a latencia de sintetizar a mesma frase a cada chamada. O motor conversacional continua local e requer Ollama + Qwen2.5 na porta 11434. Nao ha resposta artificial quando a IA estiver offline: o erro e exibido no painel.

Teste completo sintetico: `.venv\Scripts\python.exe test_jarvis_v03.py` (gera amostras de fala e testa comandos; requer internet para o gerador de vozes). Logs em `%LOCALAPPDATA%\Jarvis\jarvis.log`.


## V0.4 - Interromper a fala e lembrar do contexto

- **Interromper falando (barge-in):** o microfone permanece ativo enquanto Jarvis responde. Se detectar o inicio de uma nova fala, a reproducao da resposta atual para e a nova frase e transcrita. A resposta anterior nao e repetida nem enviada ao historico se for cancelada antes de estar pronta.
- **Protecao contra eco:** um filtro compara a voz sintetizada em reproducao com o audio captado pelo microfone e tenta evitar que Jarvis se interrompa. O filtro nao e um cancelador acustico perfeito: para jogos e caixas de som, prefira fones de ouvido ou um microfone direcional.
- **Contexto:** perguntas subsequentes utilizam um trecho limitado das ultimas conversas com a IA. Mensagens de conversas completas ficam salvas localmente em `%LOCALAPPDATA%\Jarvis\context.json` (ate 16 pares, sem gravacao de audio). O arquivo nao e enviado para o GitHub nem para servidores corporativos.
- **Apagar memoria:** use o botao "Limpar contexto" no painel para apagar o historico persistido.
- **Teste automatizado:** execute `\.venv\Scripts\python.exe test_interrupt_context.py` para testar interrupcoes rapidas, supressao de eco e memoria entre execucoes. A validacao completa da interrupcao com a sua voz real deve ser feita no microfone escolhido.

O modelo Qwen e o reconhecimento Whisper rodam localmente. A sintese de voz natural usa o servico gratuito Edge TTS pela internet e envia o texto a ser falado para esse servico.
