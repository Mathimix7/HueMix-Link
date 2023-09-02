function toggleMenu() {
    const menuIcon = document.querySelector('.menu-icon');
   const responsiveMenu = document.querySelector('.responsive-menu');

   menuIcon.classList.toggle('active');
   responsiveMenu.classList.toggle('active');
}

function closeMenuOnResize() {
   const menuIcon = document.querySelector('.menu-icon');
   const responsiveMenu = document.querySelector('.responsive-menu');

   if (window.innerWidth > 768 && responsiveMenu.classList.contains('active')) {
       menuIcon.classList.remove('active');
       responsiveMenu.classList.remove('active');
   }
}

window.addEventListener('resize', closeMenuOnResize);

function executeAlert(alertType, message){
    var previousAlert = document.querySelector(".alert");
    if (previousAlert) {
      previousAlert.remove();
    }
    var div = document.createElement("div")
    var span = document.createElement("span")
    div.className = "alert " + alertType
    div.innerHTML = "<strong> "+alertType.charAt(0).toUpperCase() + alertType.slice(1)+"!</strong> | " + message
    span.className = "closebtn"
    span.innerHTML = "&times;"
    div.appendChild(span)
    var navElement = document.querySelector("nav");
    navElement.insertAdjacentElement("afterend", div);
    var close = document.getElementsByClassName("closebtn");
    if(close.length == 1){
        div.style.opacity = "0"
        setTimeout(function(){ div.style.opacity = "1"; }, 300)
    }
    else{
        div.style.position = "fixed"
        div.style.top = "150px"
        setTimeout(function(){ div.style.top = "80px"; }, 300)
    }
    closeButtonAlert()
}

function closeButtonAlert(){
    var close = document.getElementsByClassName("closebtn");
    var i;
    for (i = 0; i < close.length; i++) {
    close[i].onclick = function(){
        var div = this.parentElement
        div.style.opacity = "0"
        setTimeout(function(){ div.remove(); }, 500)
        }
    let x = i
    let y = 0
    while (x >= 1) {
        var div = close[y].parentElement
        div.style.opacity = "0"
        setTimeout(function(){ div.remove(); }, 500)
        x--
        y++
    }
    }
}

function resetHeight() {
    const body = document.getElementsByTagName('body')[0];
    body.style.minHeight = window.innerHeight + "px";
}
window.addEventListener("load", resetHeight);
window.addEventListener("resize", resetHeight);