(function () {
	try {
		if (localStorage.getItem('ui_theme_mode') === 'dark') {
			document.body.classList.add('theme-dark');
		}
	} catch (e) {}
})();