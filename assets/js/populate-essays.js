function populateEssays(data) {
    const essaysContainer = document.getElementById('essays-container');
    const list = data.map(essay => `
        <li>
            <a href="../${essay.file_link}" target="_blank">${essay.title} - ${essay.like_count} Likes - ${essay.date}</a>
        </li>
    `).join('');
    essaysContainer.innerHTML = `<ul>${list}</ul>`;
}

document.addEventListener('DOMContentLoaded', () => {
    const embeddedDataElement = document.getElementById('essaysData');
    const essaysData = JSON.parse(embeddedDataElement.textContent);
    populateEssays(essaysData);
});
