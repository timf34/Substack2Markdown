let sortLikesAscending = false;
let sortDatesAscending = false;

function sortEssaysByDate(data) {
    sortDatesAscending = !sortDatesAscending;  // Toggle the sort order
    return data.sort((a, b) => sortDatesAscending
        ? new Date(a.date) - new Date(b.date)
        : new Date(b.date) - new Date(a.date));
}

function sortEssaysByLikes(data) {
    sortLikesAscending = !sortLikesAscending;  // Toggle the sort order
    return data.sort((a, b) => sortLikesAscending
        ? a.like_count - b.like_count
        : b.like_count - a.like_count);
}
function populateEssays(data) {
    const essaysContainer = document.getElementById('essays-container');
    const list = data.map(essay => `
        <li>
            <a href="../${essay.file_link}" target="_blank">${essay.title}</a>
            <div class="subtitle">${essay.subtitle}</div>
            <div class="metadata">${essay.like_count} Likes - ${essay.date}</div>
        </li>
    `).join('');
    essaysContainer.innerHTML = `<ul>${list}</ul>`;
}


document.addEventListener('DOMContentLoaded', () => {
    const embeddedDataElement = document.getElementById('essaysData');
    let essaysData = JSON.parse(embeddedDataElement.textContent);

    document.getElementById('sort-by-date').addEventListener('click', () => {
        populateEssays(sortEssaysByDate([...essaysData]));
    });

    document.getElementById('sort-by-likes').addEventListener('click', () => {
        populateEssays(sortEssaysByLikes([...essaysData]));
    });

    populateEssays(essaysData);
});
