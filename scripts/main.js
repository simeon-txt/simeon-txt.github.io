let minhaImagem = document.querySelector('img');

minhaImagem.onclick = function() {
    let meuSrc = minhaImagem.getAttribute('src');
    if(meuSrc === 'images/instagram-logo.png') {
      minhaImagem.setAttribute ('src','images/images/instagram-logo.png');
    } else {
      minhaImagem.setAttribute ('src','images/logo-next.jpg');
    }
}
let meuBotao = document.querySelector('button');
let meuCabecalho = document.querySelector('h1');
 function defineNomeUsuario() {
  let meuNome = prompt('Por favor, digite seu nome.');
  if(!meuNome || meuNome === null) {
    defineNomeUsuario();
  } else {
    localStorage.setItem('nome', meuNome);
    meuCabecalho.innerHTML = 'Mozilla é legal, ' + meuNome;
  }
   if(!localStorage.getItem('nome'))  {
      defineNomeUsuario();
   }
   else {
    let nomeGuardado = localStorage.getItem('nome');
    meuCabecalho.textContent = 'Instagram é legal, ' + nomeGuardado;
   }
 }
meuBotao.onclick = function() {
  defineNomeUsuario();
}
