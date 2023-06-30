from selenium import webdriver
from selenium.webdriver.common.by import By
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from time import sleep

# Setup webdriver
driver = webdriver.Edge(EdgeChromiumDriverManager().install())
# driver = webdriver.Chrome(ChromeDriverManager().install())

# Navigate to the login page
driver.get("https://substack.com/sign-in")


# First find the 'sign in with password' hyperlink and click it
signin_with_password = driver.find_element(By.XPATH, "//a[@class='login-option substack-login__login-option']")
signin_with_password.click()

# The page may take some time to load the new fields after you click the link, so let's wait for a bit.
sleep(3)

# Find the email and password fields by their 'name' attribute.
email = driver.find_element(By.NAME, "email")
password = driver.find_element(By.NAME, "password")

# Input your email and password into the fields.
email.send_keys("farrelti@tcd.ie")
password.send_keys("shopkeeper2")

# Find the submit button and click it.
submit = driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/div[3]/button")
submit.click()

sleep(5)

driver.get("https://ava.substack.com/p/the-right-conversations")

# Do some actions...
input("Press enter to continue...")
# Continue with the rest of your actions...


# Find the email and password fields. You'll need to replace 'email_field' and 'password_field'
# with the actual names or ids of the fields.
# email = driver.find_element(By.ID, "email_field")
# password = driver.find_element(By.ID, "password_field")
#
# # Input your email and password into the fields
# email.send_keys("YOUR_EMAIL")
# password.send_keys("YOUR_PASSWORD")
#
# # Find the submit button and click it. Replace 'submit_button' with the actual id or name of the button.
# submit = driver.find_element(By.ID, "submit_button")
# submit.click()
#
# # Wait for a while to let the page load or check until a certain condition is met.
# sleep(5)
#
# # Once login is successful, navigate to the next page
